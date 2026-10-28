"""The domain engine — ``MockEngine``.

The deep domain module behind the transport seam. Owns the session store, the
event fold, ``/mock/execute`` + ``/mock/observe`` dispatch, typed-RPC, status
normalization, and the inside-handler bypass lifecycle. Speaks domain types and
**raises** domain errors (``UnknownSession``, ``InvalidTurn``); it knows nothing
about HTTP, paths, or JSON. The interface is the test surface — callers drive
``create_session`` / ``set_turn`` / ``execute`` / ``observe`` / ``fold`` /
``query`` / ``events`` directly, not through wire strings.

The transport adapter (``MockService``) is the sole owner of the frozen wire
contract — ``MockService._serialize`` owns the actual bytes. This module owns the
domain log (events ARE the domain record); storing them in their wire-shaped dict
form is a locality / pass-through choice that keeps ``/events`` consumers cheap,
not a byte-identity guarantee this module enforces — a deliberate, documented
exception to "engine carries no wire shapes".
"""

import copy
import hashlib
import inspect
import json
import keyword
import logging
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from typing import Any, TypeGuard

from pydantic import BaseModel, TypeAdapter, ValidationError

from ._handler_state import _hs, _request_local
from .dialog import CallContext, PriorToolCall, SessionDialog, cap_dialog, cap_prior_calls, canonical_json
from .entity_resolution import resolve_entity
from ._typing import _resolved_type_hints, _unwrap_pydantic_class

logger = logging.getLogger(__name__)


def _remap_keyword_args(handler: Callable[..., Any], args: dict[str, Any]) -> dict[str, Any]:
    """Map wire arg keys that are Python keywords onto a handler's escaped param.

    Vendor APIs use field names that are Python keywords — notably ``from`` on
    email-send tools. A handler can't declare a ``from`` parameter, so the
    generated mock escapes it to ``from_`` (PEP 8 trailing-underscore convention).
    Without this remap the wire key ``from`` falls into the handler's ``**kwargs``
    catch-all and the escaped param keeps its default — so a required-field guard
    like ``if not from_:`` spuriously reports the field missing and the tool can
    never succeed. Only remaps a key ``k`` when ``k`` is a Python keyword, the
    handler has no ``k`` param (impossible for a keyword anyway), and it DOES
    declare a ``k_`` param; otherwise the args pass through untouched (and a
    handler relying on ``**kwargs`` for the raw key still sees it).
    """
    try:
        params = inspect.signature(handler).parameters
    except (TypeError, ValueError):
        return args
    remapped: dict[str, Any] | None = None
    for k in list(args):
        if keyword.iskeyword(k) and k not in params and f'{k}_' in params:
            if remapped is None:
                remapped = dict(args)
            remapped[f'{k}_'] = remapped.pop(k)
    return remapped if remapped is not None else args


# Resolved once at import time from package metadata so the /health endpoint
# can advertise the running version without a hardcoded string.
try:
    _MOCKENV_VERSION: str = _pkg_version('plurai-mockenv')
except PackageNotFoundError:
    _MOCKENV_VERSION = 'unknown'


class UnknownSession(Exception):  # noqa: N818 — domain name (not an "Error"-suffixed type)
    """Raised when an engine verb is asked to act on a session id that was
    never created against this engine instance.

    Carries the offending ``sid`` so the transport adapter can build the frozen
    ``{'error': "unknown session_id '<sid>'; call /session/create first",
    'status': 'error'}`` envelope without re-deriving the message.
    """

    def __init__(self, sid: str) -> None:
        self.sid = sid
        super().__init__(f'unknown session_id {sid!r}; call /session/create first')


class InvalidTurn(Exception):  # noqa: N818 — domain name, matching UnknownSession
    """Raised when a turn index is not an integer.

    A turn only earns its keep by matching the index a turn-bound check compares
    against. A string or a float stores and stamps just as happily as an int and
    then fails every one of those comparisons without a word, so the door is the
    last place a caller can still be told. ``bool`` is a subclass of ``int`` and
    is rejected with the rest.

    Carries the offending value so the transport adapter can build the frozen
    ``{'error': ..., 'status': 'error'}`` envelope without re-deriving it.
    """

    def __init__(self, turn: object) -> None:
        self.turn = turn
        super().__init__(f'turn must be an integer, got {turn!r}')


class InvalidDialog(Exception):  # noqa: N818 — domain name, matching InvalidTurn
    """Raised when a turn push's ``dialog`` or ``live_tool_calls`` does not validate. Nothing of the push is applied."""

    def __init__(self, field: str, exc: ValidationError) -> None:
        detail = '; '.join(f'{".".join(str(p) for p in err["loc"]) or field}: {err["msg"]}' for err in exc.errors(include_url=False))
        super().__init__(f'{field} is invalid: {detail}')


_LIVE_CALLS: TypeAdapter[list[PriorToolCall]] = TypeAdapter(list[PriorToolCall])


def _is_turn(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _initial_digest(initial_state: object) -> str:
    """sha256 of the canonical initial world — the replay seed of the ids minted in its sessions."""
    return hashlib.sha256(canonical_json(initial_state or {}).encode()).hexdigest()


def _mocked_content(response: object) -> Any:
    """What a mocked call returned to the agent: an envelope's ``content`` only, else the response
    without its ``_plurai_*`` keys (siblings the agent never reads)."""
    if isinstance(response, dict):
        return response['content'] if 'content' in response else {k: v for k, v in response.items() if not str(k).startswith('_plurai_')}
    return response


def _prior_tool_calls(events: list[dict], live: list[PriorToolCall], turn: int) -> list[PriorToolCall]:
    """The tool calls of turns before ``turn``: the session's mocked calls from its own event log merged
    with the live calls the harness pushed, ordered by (turn, mocked before live, position), then capped.

    An earlier call of the same turn is not a prior call, so a call repeated within a turn sees the same
    context. An observe event is not a call the mock answered; a live result arrives only as a push.
    """
    candidates: list[tuple[int, int, int, Any]] = []
    for i, ev in enumerate(list(events)):
        ev_turn = ev.get('turn')
        if ev.get('type') == 'tool_call' and _is_turn(ev_turn) and ev_turn < turn:
            candidates.append((ev_turn, 0, i, ev))
    candidates += [(call.turn, 1, i, call) for i, call in enumerate(live) if call.turn < turn]
    candidates.sort(key=lambda c: c[:3])
    calls = [
        src
        if kind
        else PriorToolCall(
            turn=c_turn,
            tool=str(src.get('tool')),
            args={str(k): v for k, v in src['args'].items()} if isinstance(src.get('args'), dict) else {},
            content=_mocked_content(src.get('response')),
        )
        for c_turn, kind, _i, src in candidates
    ]
    return cap_prior_calls(calls)


class MisaddressedWrite(Exception):  # noqa: N818 — domain name, matching UnknownSession
    """Raised when a world write's key does not name the entity its delta describes.

    A key belongs to whoever created the entity (the scenario's seed, or the handler that made
    it); to everyone else it is opaque. A write whose key was COMPUTED by a second party lands on
    a key no entity holds; the fold refuses it rather than create an untyped stray and report success.
    """


def refuse_misaddressed(state: dict, updates: dict) -> None:
    """Refuse ``updates`` before any of it lands: an untyped write to a key no entity holds, or a
    write declaring an ``_entity_type`` onto a key another type holds. Every write path calls this.
    """
    for eid, fields in (updates or {}).items():
        declared = fields.get('_entity_type') if isinstance(fields, dict) else None
        held = state.get(eid)
        if held is None and not declared:
            raise MisaddressedWrite(
                f'write to {eid!r} refused: no entity holds this key and the write declares no _entity_type. '
                'Address an existing entity by the key world_state.entities holds for it, or create one with its _entity_type.'
            )
        if held is not None and declared and isinstance(held, dict) and held.get('_entity_type') not in (None, declared):
            raise MisaddressedWrite(f'write to {eid!r} refused: the key holds a {held.get("_entity_type")!r}, the write declares a {declared!r}.')


@dataclass(frozen=True, slots=True)
class ExecuteOutcome:
    """What the adapter needs to build the ``/mock/execute`` wire response."""

    response: Any
    state_version: int


@dataclass(frozen=True, slots=True)
class ObserveOutcome:
    """What the adapter needs to build the ``/mock/observe`` wire response."""

    captured: int


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    """What a ``/session/turn`` push stored: ``None`` for a part the push did not carry."""

    dialog_entries: int | None = None
    live_tool_calls: int | None = None


#: Tools already reported as unregistered, so a per-turn dispatch loop does not re-emit the
#: same line on every call. Keyed by tool name — the set of registered handlers is fixed for a
#: service's life once building has settled, so one report per tool is the whole story.
_UNREGISTERED_REPORTED: set[str] = set()


def _warn_unregistered_tool(tool: str, registered: list[str]) -> None:
    """Say ONCE, loudly, that a tool has no handler.

    The response alone is not enough: it travels as data through several layers before anyone
    reads it, and the operator's question ("why did my mock not run?") is answered here, next
    to the list of what IS registered — which is usually the whole diagnosis, because the
    common cause is a handler module imported after the service was built.
    """
    if tool in _UNREGISTERED_REPORTED:
        return
    _UNREGISTERED_REPORTED.add(tool)
    logger.error(
        'no mock handler registered for %r — returning an ERROR, not a result. Registered: %s. '
        'A handler defined AFTER the service was built is delivered automatically; one that is '
        'never imported is not.',
        tool,
        registered or '(none)',
    )


class MockEngine:
    """The pydantic-only world-state + event-fold domain engine.

    Holds the session store and the per-instance registries — **handed in
    explicitly at construction**: by ``build_service`` (which collects from the
    ``@mock`` / ``@observe`` module-level staging registry) or by ``MockService``
    directly (explicit registries).
    The engine itself reads **no module globals**: every registry is an instance
    attribute, so two engines built with different handler sets stay isolated, and
    importing a ``mocks`` module does not affect an already-constructed engine.

    The instance registries are **mutable post-construction** and are read at
    dispatch time — ``register_mock`` / ``register_observer`` / ``register_schemas``
    called after construction take effect on the next ``execute`` / ``observe``.
    All business logic — fold, execute, observe, typed-RPC, status normalization,
    and the inside-handler bypass — lives here.
    """

    def __init__(
        self,
        *,
        handlers: dict[str, Callable] | None = None,
        observers: dict[str, Callable] | None = None,
        descriptors: dict[str, dict] | None = None,
        input_schemas: dict[str, type[BaseModel]] | None = None,
        output_schemas: dict[str, type[BaseModel]] | None = None,
        decorator_opt_out_input: set[str] | None = None,
        decorator_opt_out_output: set[str] | None = None,
    ) -> None:
        self.sessions: dict = {}
        # Instance-owned registries. Each is copied so post-construction
        # mutation (register_mock / register_schemas) never writes back into the
        # caller's dict (typically the module-level staging registry the
        # decorators populate).
        self.mock_handlers: dict = dict(handlers or {})
        self.observers: dict = dict(observers or {})
        self.descriptors: dict = dict(descriptors or {})
        # Per-instance typed-RPC registries; register_schemas() mutates these.
        # Decorator-populated entries are threaded in explicitly by the builder
        # (collected from the staging registry), NOT read from module globals.
        self.input_schemas: dict[str, type[BaseModel]] = dict(input_schemas or {})
        self.output_schemas: dict[str, type[BaseModel]] = dict(output_schemas or {})
        # Decorator opt-out sets, threaded in explicitly so register_schemas()
        # honors an explicit @mock(input_schema=None) opt-out and never fills a
        # slot the author opted out of (precedence preserved).
        self._decorator_opt_out_input: set[str] = set(decorator_opt_out_input or ())
        self._decorator_opt_out_output: set[str] = set(decorator_opt_out_output or ())
        # One lock per session guards its ``call_seq`` counter; the handler itself runs unlocked.
        self._call_locks: dict[str, threading.Lock] = {}

    # --- Registration (the service delegates here) ---

    def register_mock(self, name: str, func: Callable, descriptor: dict) -> None:
        self.mock_handlers[name] = func
        self.descriptors[name] = descriptor

    def register_observer(self, name: str, func: Callable) -> None:
        self.observers[name] = func

    def register_schemas(
        self,
        schemas: dict[str, dict[str, type[BaseModel] | None]],
    ) -> None:
        """Bulk-register input/output Pydantic schemas for typed-RPC dispatch.

        Called by a generated env's ``run.py`` after it loads the typed-RPC registry file. Three rules:

        - Decorator-registered classes WIN. The registry only fills slots the
          decorator never touched.
        - Decorator opt-outs (``@mock('foo', input_schema=None)``) ALSO win —
          the explicit opt-out blocks the YAML registry from filling that slot.
          Users can pin "this tool stays dict-only" even when a generated registry later
          has an entry for it.
        - A ``None`` direction in the YAML is a no-op — never overwrites, never
          adds an opt-out.
        """
        for tool_name, sides in (schemas or {}).items():
            in_cls = (sides or {}).get('input')
            out_cls = (sides or {}).get('output')
            if in_cls is not None and tool_name not in self.input_schemas and tool_name not in self._decorator_opt_out_input:
                self.input_schemas[tool_name] = in_cls
            if out_cls is not None and tool_name not in self.output_schemas and tool_name not in self._decorator_opt_out_output:
                self.output_schemas[tool_name] = out_cls

    # --- Session lifecycle ---

    def create_session(self, sid: str, *, initial_state: dict, agent_context: dict) -> None:
        self.sessions[sid] = {
            'initial_state': initial_state or {},
            'agent_context': agent_context or {},
            'events': [],
            # A session starts on turn 0 so a caller that never sets a turn gets
            # the same stamp an explicit ``turn=0`` would give it.
            'current_turn': 0,
            # The replay seed of the ids its handlers mint: the same initial world and the same call
            # order mint the same ids, in any session or process.
            'initial_digest': _initial_digest(initial_state),
            'call_seq': 0,
            'dialog': SessionDialog(),
            'live_tool_calls': [],
        }

    def set_turn(self, sid: str, turn: object, *, dialog: object = None, live_tool_calls: object = None) -> TurnOutcome:
        """Record the turn that later ``execute`` calls inherit.

        The harness knows the turn index; an agent running on someone else's
        servers does not. Holding the turn on the session lets the harness set
        it once per user turn and lets an external caller omit it entirely, so
        turn-bound deterministic checks still see correctly labelled events.

        Takes ``turn`` as ``object`` because the value arrives off the wire:
        the session check runs first, so a session-keyed caller fails the way
        every other session-keyed route fails, and the type check follows.

        ``dialog`` (a :class:`SessionDialog`) and ``live_tool_calls`` (a list of
        :class:`PriorToolCall` the harness really executed) are the conversation
        so far, pushed as interpretation context for a handler — never world
        rows. Each push is cumulative and REPLACES what the session held, so a
        retried push is idempotent; each is capped here, whoever sent it. A
        part the push omits (or sends as ``None``) is left as it was. Every part
        is validated before any is applied.
        """
        if sid not in self.sessions:
            raise UnknownSession(sid)
        if isinstance(turn, bool) or not isinstance(turn, int):
            raise InvalidTurn(turn)
        stored_dialog = stored_live = None
        if dialog is not None:
            try:
                pushed = SessionDialog.model_validate(dialog)
            except ValidationError as exc:
                raise InvalidDialog('dialog', exc) from exc
            capped = cap_dialog(pushed.entries)
            stored_dialog = capped.model_copy(update={'dropped_entries': pushed.dropped_entries + capped.dropped_entries})
        if live_tool_calls is not None:
            try:
                stored_live = cap_prior_calls(_LIVE_CALLS.validate_python(live_tool_calls))
            except ValidationError as exc:
                raise InvalidDialog('live_tool_calls', exc) from exc
        session = self.sessions[sid]
        session['current_turn'] = turn
        if stored_dialog is not None:
            session['dialog'] = stored_dialog
        if stored_live is not None:
            session['live_tool_calls'] = stored_live
        return TurnOutcome(
            dialog_entries=None if stored_dialog is None else len(stored_dialog.entries),
            live_tool_calls=None if stored_live is None else len(stored_live),
        )

    def health(self) -> dict:
        return {'status': 'ok', 'version': _MOCKENV_VERSION, 'mocks': list(self.mock_handlers.keys())}

    # --- Event fold ---

    def fold(self, sid: str) -> dict:
        """The session's world now: the initial state with every event replayed in order.

        Shares NO mutable object with the session record. Every per-call ``hs.state`` is built here,
        so an alias would let a later call rewrite an earlier event: a row created in one call and
        edited in a later one would be recorded with the later values, in the log and in every
        ``/events`` answer. Deep, not shallow: rows nest lists (tags, line items) that an edit changes in place.
        """
        session = self.sessions.get(sid, {})
        state = {}
        for eid, edata in session.get('initial_state', {}).get('entities', {}).items():
            state[eid] = copy.deepcopy(edata)
        for event in session.get('events', []):
            for eid, delta in event.get('state_delta', {}).items():
                if eid not in state:
                    state[eid] = {}
                state[eid].update(copy.deepcopy(delta))
            for eid, edata in event.get('new_entities', {}).items():
                state[eid] = copy.deepcopy(edata)
        return state

    # --- Mock execute (incl. typed-RPC) ---

    def execute(self, sid: str, tool: str, args: dict, *, turn: int | None = None) -> ExecuteOutcome:
        # Defensive: stale subprocess from a prior run may hit us with a session
        # id we never created. Raise a domain error instead of letting the
        # unguarded ``self.sessions[sid]["events"].append`` below raise KeyError.
        if sid not in self.sessions:
            raise UnknownSession(sid)
        # An explicit turn wins; omitting it inherits whatever the session is
        # on. ``sessions`` is a public mapping that callers seed directly, so a
        # session dict here need not have come from ``create_session`` and need
        # not carry the key; those sessions read as turn 0.
        stamped_turn = self.sessions[sid].get('current_turn', 0) if turn is None else turn
        current = self.fold(sid)

        handler = self.mock_handlers.get(tool)
        # Typed-RPC — when a Pydantic input schema is
        # registered, validate args before dispatch. On success the handler
        # receives either the typed object (when its sole non-self/cls/ctx param
        # matches input_cls) or **args dict. On ValidationError,
        # fall back to dict and tag the event so consumers can spot the drift.
        input_cls = self.input_schemas.get(tool)
        output_cls = self.output_schemas.get(tool)
        typed_input: BaseModel | None = None
        typed_param_name: str | None = None
        shape = 'dict'
        if input_cls is not None and handler is not None:
            # Identify the handler parameter whose annotation IS input_cls so
            # the dispatch can route the typed payload to the right kwarg even
            # for mixed signatures like ``(item_id: str, request: Model)``.
            handler_sig_for_typed = inspect.signature(handler)
            handler_hints_for_typed = _resolved_type_hints(handler)
            for name in handler_sig_for_typed.parameters:
                if name in ('self', 'cls', 'ctx'):
                    continue
                if _unwrap_pydantic_class(handler_hints_for_typed.get(name)) is input_cls:
                    typed_param_name = name
                    break
            # Wire conventions, in priority order:
            #   1. Nested: args[typed_param_name] is a dict carrying the model
            #      payload (the canonical shape for mixed-signature handlers +
            #      LangChain's natural JSON serialization for Pydantic tool
            #      inputs).
            #   2. Flat: args itself is the model payload (single-Pydantic-param
            #      handlers where the caller spread the fields
            #      directly).
            # ValidationError is the common case, but Pydantic custom validators
            # can raise plain ValueError / AssertionError / TypeError that aren't
            # wrapped — treat all of those as "schema rejected the input" so they
            # don't get confused with handler-raised errors (which fall through
            # into the broader ``_plurai_handler_error`` sentinel below).
            nested_payload = args.get(typed_param_name) if typed_param_name and isinstance(args, dict) else None
            payload = nested_payload if isinstance(nested_payload, dict) else args
            try:
                typed_input = input_cls.model_validate(payload)
                shape = 'typed'
            except (ValidationError, ValueError, TypeError, AssertionError) as ve:
                logger.warning(
                    'typed-RPC validation failed for %r: %s: %s; falling back to dict dispatch',
                    tool,
                    type(ve).__name__,
                    ve,
                )
                shape = 'dict_validation_failed'
        if handler:
            call = self._begin_call(sid, stamped_turn)
            hs = _hs()
            hs.state = current
            hs.context = self.sessions.get(sid, {}).get('agent_context', {})
            hs.updates = {}
            hs.new_entities = {}
            hs.refused = None
            hs.call = call
            hs.minted = 0
            _request_local.inside = True
            try:
                if typed_input is not None and typed_param_name is not None:
                    # Build the kwargs handler actually accepts: the typed param
                    # gets the validated model; other scalar kwargs pass through
                    # from args but only those whose names are real handler
                    # params (so flattened Pydantic fields like ``name``/
                    # ``count`` don't leak when the handler only takes
                    # ``request``).
                    handler_params = set(handler_sig_for_typed.parameters)
                    scalar_kwargs = {k: v for k, v in args.items() if k in handler_params and k != typed_param_name}
                    response = handler(
                        **scalar_kwargs,
                        **{typed_param_name: typed_input},
                    )
                else:
                    response = handler(**_remap_keyword_args(handler, args))
            except Exception as e:  # noqa: BLE001 — handler errors become a tagged response, never propagate
                # Mark handler-raised errors with a private sentinel key so the
                # client-side resolver can tell them apart from a real handler
                # returning ``{"error": "..."}`` (which is legitimate — some
                # tools' real return shape includes an error field). The resolver
                # substitutes a schema stub for handler-raised errors only.
                # Also log with the traceback so the failure is debuggable in
                # logs (the wrapped response shape carries only the type + str).
                logger.exception('mock handler %r raised; tagging as handler error', tool)
                response = {
                    '_plurai_handler_error': True,
                    'error': f'{type(e).__name__}: {e}',
                }
            finally:
                _request_local.inside = False
                hs.call = None
            state_delta = hs.updates
            new_entities = hs.new_entities
            if hs.refused is not None:
                # A refused write voids the whole call, even one the handler caught: none of its
                # writes land, and the tag tells the caller the fault is the writer's key, not the tool.
                response = {'_plurai_handler_error': True, '_plurai_write_refused': True, 'error': f'MisaddressedWrite: {hs.refused}'}
                state_delta, new_entities = {}, {}
        else:
            # NO HANDLER for this tool. An 'ok' answer would be indistinguishable from a real
            # success to every consumer downstream: an error-rate policy would count it clean,
            # a report of executed tools would list it, and a deterministic
            # "was X invoked" check would pass. A scenario whose target is "call X and report
            # what it returned" would then be satisfiable only by invention.
            #
            # A missing handler is a HARNESS defect, so it is reported as an error rather than
            # as a result. ``error`` is the existing marker the status normalisation below
            # already reads, and every consumer that understands a failed tool
            # understands this one. ``_plurai_unregistered_tool`` lets a caller tell it apart
            # from a handler that legitimately returned an error.
            _warn_unregistered_tool(tool, sorted(self.mock_handlers))
            response = {
                'status': 'error',
                'error': f'no mock handler is registered for {tool!r}',
                '_plurai_unregistered_tool': True,
                'tool': tool,
            }
            state_delta = {}
            new_entities = {}

        # Pydantic response → dict before recording, so every field stays JSON-serialisable; the typed FQN goes into typed_output_class.
        if isinstance(response, BaseModel):
            response = response.model_dump(mode='json')

        # Normalised status so the backend's code-eval policies don't have to
        # introspect the response shape to tell success from error. 'error' when
        # the handler raised (we wrapped it in _plurai_handler_error) OR when the
        # response carries an 'error' field at the top level (legitimate
        # handler-returned error responses).
        status = 'error' if isinstance(response, dict) and (response.get('_plurai_handler_error') or response.get('error')) else 'ok'
        self.sessions[sid]['events'].append(
            {
                'type': 'tool_call',
                'tool': tool,
                'args': args,
                'response': response,
                'status': status,
                # Snapshots, not the handler's objects: a row it keeps a reference to (a module-level
                # cache) and edits on a later call must not rewrite this event.
                'state_delta': copy.deepcopy(state_delta),
                'new_entities': copy.deepcopy(new_entities),
                'turn': stamped_turn,
                # Typed-RPC discriminator + FQNs. All optional; "dict" / None means the dict path.
                'shape': shape,
                'typed_input_class': (f'{input_cls.__module__}.{input_cls.__qualname__}' if input_cls is not None else None),
                'typed_output_class': (f'{output_cls.__module__}.{output_cls.__qualname__}' if output_cls is not None else None),
            }
        )
        print(f'  [plurai] MOCK {tool}({json.dumps(args, default=str)}) → {json.dumps(response, default=str)[:100]}', file=sys.stderr)
        return ExecuteOutcome(response=response, state_version=len(self.sessions[sid]['events']))

    def _begin_call(self, sid: str, turn: object) -> CallContext:
        """Take this call's ``call_seq`` under the session's lock, and freeze what its handler may read.

        ``call_seq`` — not the event count — seeds the ids the handler mints: two calls in flight at once
        in one session see the same event count, and would mint the same id.
        """
        session = self.sessions[sid]
        with self._call_locks.setdefault(sid, threading.Lock()):
            call_seq = session.get('call_seq', 0)
            session['call_seq'] = call_seq + 1
        if 'initial_digest' not in session:  # a session seeded straight into the mapping
            session['initial_digest'] = _initial_digest(session.get('initial_state'))
        ctx_turn = turn if _is_turn(turn) else session.get('current_turn', 0)
        return CallContext(
            session_id=sid,
            turn=ctx_turn,
            call_seq=call_seq,
            initial_digest=session['initial_digest'],
            # A copy: a handler that mutates its dialog in place must not change the session's.
            dialog=(session.get('dialog') or SessionDialog()).model_copy(deep=True),
            prior_tool_calls=_prior_tool_calls(session.get('events', []), session.get('live_tool_calls', []), ctx_turn),
        )

    # --- Observe ---

    def observe(self, sid: str, tool: str, args: dict, result: Any) -> ObserveOutcome:
        # Defensive: see execute() above for rationale.
        if sid not in self.sessions:
            raise UnknownSession(sid)
        observer = self.observers.get(tool)
        new_entities: dict = {}
        if observer:
            hs = _hs()
            hs.state = self.fold(sid)
            hs.new_entities = {}
            _request_local.inside = True
            try:
                observer(args, result)
                new_entities = hs.new_entities
            except Exception:  # noqa: BLE001 — observe failures must never break a run
                # The observe event record has no failure field (adding one
                # changes the wire), so the failure is traced in the logs.
                logger.warning('observer %r failed; observe event recorded with no captures', tool, exc_info=True)
            finally:
                _request_local.inside = False
        self.sessions[sid]['events'].append(
            {
                'type': 'observe',
                'tool': tool,
                'args': args,
                'result': result,
                'new_entities': copy.deepcopy(new_entities),  # a snapshot, as in execute()
                'state_delta': {},
            }
        )
        print(f'  [plurai] OBSERVE {tool} — captured {len(new_entities)} entities', file=sys.stderr)
        return ObserveOutcome(captured=len(new_entities))

    # --- State reads ---

    def context(self, sid: str) -> dict:
        return self.sessions.get(sid, {}).get('agent_context', {})

    def query(self, sid: str, entity_type: str | None, **filters: str) -> list[dict]:
        state = self.fold(sid)
        results = []
        for e in state.values():
            if isinstance(e, dict) and e.get('_entity_type') == entity_type:
                match = True
                for k, v in filters.items():
                    if str(e.get(k)) != v:
                        match = False
                if match:
                    results.append(e)
        return results

    def entity(self, sid: str, eid: str, entity_type: str | None = None) -> dict | None:
        return resolve_entity(self.fold(sid), eid, entity_type)

    def events(self, sid: str) -> list[dict]:
        return self.sessions.get(sid, {}).get('events', [])

    # --- State update ---

    def update_state(self, sid: str, updates: dict, source: str = 'external') -> int:
        # Defensive: a stale agent subprocess from a previous run can keep
        # hitting the long-running mock service with a session id whose
        # ``/session/create`` was never called against this server instance
        # (mock restarted, sessions dict reset, etc.). Raising a domain error is
        # strictly better than letting the request thread crash with KeyError.
        if sid not in self.sessions:
            raise UnknownSession(sid)
        refuse_misaddressed(self.fold(sid), updates)
        self.sessions[sid]['events'].append(
            {
                'type': 'state_update',
                'source': source,
                'state_delta': copy.deepcopy(updates),  # a snapshot: the caller keeps its dict
                'new_entities': {},
            }
        )
        return len(self.sessions[sid]['events'])
