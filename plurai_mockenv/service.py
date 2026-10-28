"""The transport adapter — ``MockService``.

The thin module at the transport seam. Maps the HTTP wire ⇄ :class:`MockEngine`
calls and is the **sole owner of the frozen wire contract** — request parsing,
response/event serialization, error-envelope mapping, and the serve lifecycle
(``serve_forever`` / ``start`` / ``stop``). It is a FastAPI app served by
uvicorn; the engine is unaware of that choice.

``_handle(self, method, path, body) -> dict`` is the **internal** dispatcher the
FastAPI funnel calls — NOT a public override hook. Its dispatch is the frozen
``/mock/*`` wire router and stays byte-identical. An in-process caller can invoke
``service._handle('POST', '/mock/execute', body)`` directly to exercise the wire
router without a socket. Built-in behavior (the
``exposed_state_fields`` projection) is wired into the adapter via constructor
config — ``build_service`` or the ``MockService`` constructor — not by subclassing.
"""

import asyncio
import functools
import json
import logging
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import parse_qs, urlparse

import anyio
import anyio.to_thread
import uvicorn
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel

from .entity_resolution import AmbiguousEntityId
from ._engine import (
    InvalidDialog,
    InvalidTurn,
    MisaddressedWrite,
    MockEngine,
    UnknownSession,
    _MOCKENV_VERSION,  # noqa: F401 — re-exported for tests and callers that read the version here
)

logger = logging.getLogger(__name__)

# The frozen status VALUE for the error envelope. Engine raises domain errors;
# the adapter is the sole owner of the wire shape — ``{'error': ...,
# 'status': 'error'}`` (the ``'status'`` field is this literal ``'error'``, NOT
# a ``status_code`` key).
_UNKNOWN_SESSION_STATUS = 'error'

#: Whitelist sections the projection does not serve. A whitelist that declares one is accepted:
#: the section is ignored with a warning at load rather than refused.
_RETIRED_EXPOSED_SECTIONS = frozenset({'conversation'})

#: Requests one mock runs at once. A runaway guard, not a sizing: AnyIO's default of 40 lets slow handlers
#: (a long live call) queue fast ones (a cache hit) behind them, so the bound must sit far above real fan-out.
MAX_CONCURRENT_CALLS = 256


class _ThreadedUvicornServer(uvicorn.Server):
    """uvicorn server that skips signal-handler installation.

    ``uvicorn.Server.run`` installs SIGINT/SIGTERM handlers via
    ``signal.signal``, which raises ``ValueError`` off the main thread. Used
    ONLY by the non-blocking ``start()`` path, which runs the server in a daemon
    thread; we override the install to a no-op and drive shutdown via ``stop()``
    instead. ``serve_forever`` deliberately uses the base ``uvicorn.Server`` on
    the main thread, where signal handlers are valid.
    """

    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.startup_exc: BaseException | None = None

    def install_signal_handlers(self) -> None:
        return

    def run_capturing(self) -> None:
        """Thread target that captures a startup failure (e.g. port-in-use)
        instead of letting it die as an unhandled thread exception. ``start()``
        re-raises it on the caller's thread."""
        try:
            self.run()
        except BaseException as exc:  # noqa: BLE001 — surfaced synchronously by start()
            self.startup_exc = exc


class MockService:
    def __init__(
        self,
        port: int = 8199,
        host: str = '127.0.0.1',
        *,
        handlers: dict[str, Callable] | None = None,
        observers: dict[str, Callable] | None = None,
        descriptors: dict[str, dict] | None = None,
        schemas: dict[str, dict[str, type[BaseModel] | None]] | None = None,
        decorator_opt_out: dict[str, Iterable[str]] | None = None,
        exposed_state_fields: Mapping[str, Iterable[str]] | None = None,
        max_concurrent_calls: int = MAX_CONCURRENT_CALLS,
    ):
        """Construct a ready-but-unserved mock service.

        Every registry is passed **explicitly** — the engine reads no module
        globals. ``build_service`` is the canonical constructor for the
        decorator-staging path (it collects the ``@mock`` / ``@observe`` staging
        registry and hands it in here); construct ``MockService`` directly to pass
        an explicit registry set (tests, programmatic handlers).

        - ``handlers`` / ``observers`` / ``descriptors`` — the mock + observer
          handler maps and tool descriptors.
        - ``schemas`` — typed-RPC input/output Pydantic classes, shape
          ``{tool: {'input': Model|None, 'output': Model|None}}``.
        - ``decorator_opt_out`` — explicit ``@mock(input_schema=None)`` opt-outs,
          shape ``{'input': {tool, ...}, 'output': {tool, ...}}``, threaded so
          ``register_schemas`` won't later fill an opted-out slot.
        - ``exposed_state_fields`` — the whitelist
          (``{'evaluation': [path, ...]}``) driving the built-in
          ``GET /state/{sid}/exposed`` + ``POST /eval/expose`` projection.
          ``None`` disables those built-in routes. A ``conversation``
          section is ignored with a warning.
        - ``max_concurrent_calls`` — how many requests run their handler at once
          (worker threads); the rest wait for admission.
        """
        self.port = port
        self.host = host
        # Split the typed-RPC schemas into the per-direction maps the engine
        # holds. ``None`` directions are skipped (no-op, same as register_schemas).
        # Asymmetry is intentional: constructor-passed ``schemas`` are explicit and
        # always win; ``decorator_opt_out`` only gates the post-construction YAML
        # ``register_schemas`` fill, not the explicit schemas installed here.
        input_schemas: dict[str, type[BaseModel]] = {}
        output_schemas: dict[str, type[BaseModel]] = {}
        for tool_name, sides in (schemas or {}).items():
            in_cls = (sides or {}).get('input')
            out_cls = (sides or {}).get('output')
            if in_cls is not None:
                input_schemas[tool_name] = in_cls
            if out_cls is not None:
                output_schemas[tool_name] = out_cls
        opt_out = decorator_opt_out or {}
        self._engine = MockEngine(
            handlers=handlers,
            observers=observers,
            descriptors=descriptors,
            input_schemas=input_schemas,
            output_schemas=output_schemas,
            decorator_opt_out_input=set(opt_out.get('input', ())),
            decorator_opt_out_output=set(opt_out.get('output', ())),
        )
        # Built-in exposed-fields projection state. ``_exposed_fields`` is the
        # configured whitelist (None ⇒ the built-in routes are not
        # served); ``_runtime_aliases`` accrues POST /eval/expose entries.
        # Only sequence-valued entries are projection sections: callers (and
        # env generators) may include scalar metadata keys alongside the
        # sections — most commonly a top-level ``version:`` — and those must be
        # tolerated, not iterated. Without this guard a ``version: 1`` reaches
        # ``list(1)`` and raises ``TypeError: 'int' object is not iterable``,
        # crashing the service on startup. ``str``/``bytes`` are intentionally
        # excluded too (a bare string is a misconfigured section, not paths).
        self._exposed_fields: dict[str, list[str]] | None = (
            {section: list(paths) for section, paths in exposed_state_fields.items() if isinstance(paths, (list, tuple, set))}
            if exposed_state_fields is not None
            else None
        )
        retired = sorted(_RETIRED_EXPOSED_SECTIONS.intersection(exposed_state_fields or ()))
        if retired:
            logger.warning(
                'exposed_state_fields section(s) %s are retired and ignored; remove them from the whitelist.',
                retired,
            )
        self._runtime_aliases: list[dict[str, str]] = []
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._max_concurrent_calls = max_concurrent_calls
        # Built lazily on the serving loop (AnyIO limiters are loop-bound); a restart on a new loop gets a fresh one.
        self._limiter: anyio.CapacityLimiter | None = None
        self._limiter_loop: asyncio.AbstractEventLoop | None = None
        self._app = self._build_app()

    # --- Engine-backed attributes: instance properties over the single engine-owned store (``svc.sessions``, ``svc.mock_handlers``, ...). ---

    @property
    def sessions(self) -> dict:
        return self._engine.sessions

    @property
    def mock_handlers(self) -> dict:
        return self._engine.mock_handlers

    @property
    def observers(self) -> dict:
        return self._engine.observers

    @property
    def descriptors(self) -> dict:
        return self._engine.descriptors

    @property
    def input_schemas(self) -> dict[str, type[BaseModel]]:
        return self._engine.input_schemas

    @property
    def output_schemas(self) -> dict[str, type[BaseModel]]:
        return self._engine.output_schemas

    # --- Registration (delegate to the engine) ---

    def register_mock(self, name: str, func: Callable, descriptor: dict) -> None:
        self._engine.register_mock(name, func, descriptor)

    def register_observer(self, name: str, func: Callable) -> None:
        self._engine.register_observer(name, func)

    def register_schemas(
        self,
        schemas: dict[str, dict[str, type[BaseModel] | None]],
    ) -> None:
        """Bulk-register input/output Pydantic schemas for typed-RPC dispatch.

        Delegates to the engine. Precedence rules: decorator wins; an explicit
        ``None`` opt-out blocks the YAML fill; a ``None`` direction is a no-op.
        Called BEFORE ``serve_forever()`` in generated
        run.py — schemas registered after construction are honored by execute
        dispatch because the engine reads its registries at dispatch time.
        """
        self._engine.register_schemas(schemas)

    def _fold_state(self, sid: str) -> dict:
        """Convenience accessor: the fold lives on the engine; this forwards to it
        for callers that read ``service._fold_state`` directly."""
        return self._engine.fold(sid)

    def _handle(self, method: str, path: str, body: dict) -> dict:
        """Internal dispatcher + frozen wire-shape router.

        The FastAPI funnel reconstructs ``(method, path, body)`` and calls this;
        it is NOT a public override hook. The hand-rolled path-string dispatch
        (including the ``/state/`` ordering and ``p.count('/')`` discrimination)
        is the frozen ``/mock/*`` wire and stays byte-identical. Each route body
        delegates to ``self._engine.<verb>(...)`` and translates engine
        exceptions into the frozen ``{error, status}`` envelope. The built-in
        ``exposed_state_fields`` routes are checked BEFORE the generic ``/state/``
        branches so ``/state/{sid}/exposed`` is not shadowed by the single-entity
        branch.
        """
        parsed = urlparse(path)
        p = parsed.path
        qs = parse_qs(parsed.query)

        # --- Health ---
        if p == '/health':
            return self._engine.health()

        # --- Built-in exposed-fields projection (BEFORE the /state/ branches so
        # /state/{sid}/exposed is not shadowed by the single-entity branch). Only
        # served when exposed_state_fields was configured at build. ---
        if self._exposed_fields is not None:
            if method == 'GET' and p.startswith('/state/') and p.endswith('/exposed'):
                return self._project_exposed(p.split('/')[2])
            if method == 'POST' and p.rstrip('/') == '/eval/expose':
                return self._handle_expose(body)

        # --- Session ---
        if method == 'POST' and p == '/session/create':
            sid = body['session_id']
            self._engine.create_session(
                sid,
                initial_state=body.get('initial_state', {}),
                agent_context=body.get('agent_context', {}),
            )
            return {'session_id': sid, 'status': 'created'}

        # --- Session turn ---
        if method == 'POST' and p == '/session/turn':
            sid = body['session_id']
            # ``.get`` rather than ``[...]``: an omitted turn is a caller error
            # this route can name, not a bare KeyError off the funnel.
            turn = body.get('turn')
            try:
                stored = self._engine.set_turn(sid, turn, dialog=body.get('dialog'), live_tool_calls=body.get('live_tool_calls'))
            except (UnknownSession, InvalidTurn, InvalidDialog) as exc:
                return {'error': str(exc), 'status': _UNKNOWN_SESSION_STATUS}
            out: dict[str, Any] = {'session_id': sid, 'turn': turn, 'status': 'turn_set'}
            # Acknowledged only when pushed, so a body with neither carries neither key.
            if stored.dialog_entries is not None:
                out['dialog_entries'] = stored.dialog_entries
            if stored.live_tool_calls is not None:
                out['live_tool_calls_stored'] = stored.live_tool_calls
            return out

        # --- Mock execute ---
        if method == 'POST' and p == '/mock/execute':
            sid = body['session_id']
            tool = body['tool']
            args = body.get('args', {})
            try:
                outcome = self._engine.execute(sid, tool, args, turn=body.get('turn'))
            except UnknownSession as exc:
                return {'error': str(exc), 'status': _UNKNOWN_SESSION_STATUS}
            return {'response': outcome.response, 'state_version': outcome.state_version}

        # --- Observe ---
        if method == 'POST' and p == '/mock/observe':
            sid = body['session_id']
            tool = body['tool']
            try:
                outcome = self._engine.observe(sid, tool, body.get('args', {}), body.get('result'))
            except UnknownSession as exc:
                return {'error': str(exc), 'status': _UNKNOWN_SESSION_STATUS}
            return {'status': 'observed', 'captured': outcome.captured}

        # --- State: context (check BEFORE generic entity) ---
        if method == 'GET' and p.startswith('/state/') and p.endswith('/context'):
            sid = p.split('/')[2]
            return self._engine.context(sid)

        # --- State: query (check BEFORE generic entity) ---
        if method == 'GET' and p.startswith('/state/') and '/query' in p:
            sid = p.split('/')[2]
            etype = qs.get('entity_type', [None])[0]
            # Exclude the names bound to ``query``'s positional params so a
            # crafted query string like ``?sid=x`` can't collide with the
            # ``query(sid, entity_type, **filters)`` dispatch and raise
            # ``TypeError: got multiple values for argument``.
            filters = {k: vs[0] for k, vs in qs.items() if k not in ('entity_type', 'sid')}
            return {'results': self._engine.query(sid, etype, **filters)}

        # --- State: update (check BEFORE generic entity) ---
        if method == 'POST' and p.startswith('/state/') and p.endswith('/update'):
            sid = p.split('/')[2]
            updates = body.get('updates', {})
            source = body.get('source', 'external')
            try:
                state_version = self._engine.update_state(sid, updates, source)
            except UnknownSession as exc:
                return {'error': str(exc), 'status': _UNKNOWN_SESSION_STATUS}
            except MisaddressedWrite as exc:
                return {'error': str(exc), 'status': 'write_refused'}
            return {'status': 'updated', 'state_version': state_version}

        # --- State: full ---
        if method == 'GET' and p.startswith('/state/') and p.count('/') == 2:
            sid = p.split('/')[2]
            return {'session_id': sid, 'state': self._engine.fold(sid)}

        # --- State: single entity ---
        if method == 'GET' and p.startswith('/state/') and p.count('/') == 3:
            parts = p.split('/')
            sid, eid = parts[2], parts[3]
            try:
                return {'entity': self._engine.entity(sid, eid, qs.get('entity_type', [None])[0])}
            except AmbiguousEntityId as exc:
                return {'error': str(exc), 'status': _UNKNOWN_SESSION_STATUS}

        # --- Events ---
        if method == 'GET' and p.startswith('/events/'):
            sid = p.split('/')[2]
            return {'session_id': sid, 'events': self._engine.events(sid)}

        return {'error': f'Unknown: {method} {path}'}

    # --- Built-in exposed-fields projection (exposed-projection-v1) ---
    #
    # Canonical shape DEFINED here. The GET /state/{sid}/exposed response carries:
    # session_id, a fixed projection_version marker 'exposed-projection-v1', an
    # 'evaluation' map (the configured whitelist section projected from the folded
    # session state, keyed by its dotted path), and a 'runtime' map (paths added via
    # POST /eval/expose, keyed by their alias). POST /eval/expose
    # takes body {'expose_fields': [{'path': ..., 'alias': ...}]} and acks with
    # {'status': 'ok', 'count': <total runtime aliases>}. A whitelisted or
    # runtime path that does not resolve in the current fold is OMITTED (absent,
    # not null) so the projection only ever carries fields that are present.

    @staticmethod
    def _resolve_path(state: dict, dotted: str) -> tuple[bool, Any]:
        """Walk a dotted path into the folded state. Returns (found, value).

        Each segment indexes into a Mapping; a missing segment or a non-Mapping
        intermediate yields (False, None) so the caller omits the field.
        """
        cur: Any = state
        for segment in dotted.split('.'):
            if isinstance(cur, Mapping) and segment in cur:
                cur = cur[segment]
            else:
                return False, None
        return True, cur

    def _project_section(self, state: dict, paths: Iterable[str]) -> dict:
        out: dict[str, Any] = {}
        for dotted in paths:
            found, value = self._resolve_path(state, dotted)
            if found:
                out[dotted] = value
        return out

    def _project_exposed(self, sid: str) -> dict:
        # Caller only reaches here when self._exposed_fields is not None; fall
        # back to an empty whitelist defensively rather than assert.
        whitelist = self._exposed_fields or {}
        state = self._engine.fold(sid)
        runtime: dict[str, Any] = {}
        for entry in self._runtime_aliases:
            found, value = self._resolve_path(state, entry['path'])
            if found:
                runtime[entry['alias']] = value
        return {
            'session_id': sid,
            'projection_version': 'exposed-projection-v1',
            'evaluation': self._project_section(state, whitelist.get('evaluation', [])),
            'runtime': runtime,
        }

    def _handle_expose(self, body: dict) -> dict:
        entries = body.get('expose_fields') or []
        if not isinstance(entries, list):
            return {'status': 'error', 'message': 'expose_fields must be a list'}
        for entry in entries:
            if isinstance(entry, dict) and 'path' in entry:
                path = entry['path']
                self._runtime_aliases.append({'path': path, 'alias': entry.get('alias') or path})
        return {'status': 'ok', 'count': len(self._runtime_aliases)}

    # --- FastAPI transport ---

    def _build_app(self) -> FastAPI:
        """Build the FastAPI app whose ONLY job is to funnel every request
        through ``self._handle``.

        A single catch-all route per HTTP method reconstructs
        ``(method, path, body)`` and calls ``self._handle`` on a worker thread
        (via ``anyio.to_thread.run_sync`` under this service's own limiter of
        ``max_concurrent_calls``, not AnyIO's default of 40) for two reasons: (1) the per-request
        thread-local bypass state (``_hs`` / ``_request_local.inside``) keeps its
        one-thread-per-request isolation, and (2) ``_handle`` runs synchronous
        user mock/observe code that must not block the asyncio event loop.
        The reconstructed ``path`` carries the raw query string so the string
        router's ``urlparse``/``parse_qs`` logic is unchanged.
        """
        app = FastAPI()

        async def run_handler(method: str, path: str, body: dict) -> dict:
            loop = asyncio.get_running_loop()
            if self._limiter is None or self._limiter_loop is not loop:
                self._limiter = anyio.CapacityLimiter(self._max_concurrent_calls)
                self._limiter_loop = loop
            return await anyio.to_thread.run_sync(functools.partial(self._handle, method, path, body), limiter=self._limiter)

        @app.get('/{full_path:path}')
        async def _catch_get(full_path: str, request: Request) -> Response:
            path = request.url.path
            if request.url.query:
                path = f'{path}?{request.url.query}'
            try:
                data = await run_handler('GET', path, {})
            except Exception as e:  # noqa: BLE001 — adapter owns the wire envelope; never leak a bare 500
                logger.exception('unexpected error handling GET %s', path)
                data = {'error': f'{type(e).__name__}: {e}', 'status': 'error'}
            return self._serialize(data)

        @app.post('/{full_path:path}')
        async def _catch_post(full_path: str, request: Request) -> Response:
            raw = await request.body()
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError as e:
                return self._serialize({'error': f'invalid JSON body: {e}', 'status': 'error'})
            path = request.url.path
            if request.url.query:
                path = f'{path}?{request.url.query}'
            try:
                data = await run_handler('POST', path, body)
            except Exception as e:  # noqa: BLE001 — adapter owns the wire envelope; never leak a bare 500
                # Covers KeyError from a missing session_id/tool plus any
                # non-UnknownSession engine bug. UnknownSession is already mapped
                # to the envelope inside _handle; this is the defensive backstop
                # so pathological input returns 200 + {error,status:'error'}.
                logger.exception('unexpected error handling POST %s', path)
                data = {'error': f'{type(e).__name__}: {e}', 'status': 'error'}
            return self._serialize(data)

        return app

    @staticmethod
    def _serialize(data: dict) -> Response:
        body = json.dumps(data, default=str).encode()
        return Response(content=body, media_type='application/json')

    def start(self) -> None:
        """Non-blocking: start the uvicorn server in a daemon thread and return.

        SDK local mode calls this, then ``stop()`` at exit.
        """
        config = uvicorn.Config(self._app, host=self.host, port=self.port, log_level='warning')
        self._server = _ThreadedUvicornServer(config)
        self._thread = threading.Thread(target=self._server.run_capturing, daemon=True)
        self._thread.start()
        # Wait briefly for the server loop to come up so a caller that issues a
        # request immediately after start() doesn't race the bind.
        deadline = time.monotonic() + 5.0
        while not self._server.started and self._thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        # Surface a bind failure on the CALLER's thread. uvicorn's bind failure
        # dies inside the daemon thread, so without this check start() would
        # return normally and print a "listening" banner for a server that
        # never bound. SDK local mode (start() then issue requests) depends on the
        # synchronous failure.
        if not self._server.started:
            startup_exc = self._server.startup_exc
            self._thread = None
            self._server = None
            raise RuntimeError(f'mock service failed to start (bind {self.host}:{self.port}?) within 5s') from startup_exc
        print(f'[plurai] Mock service at http://{self.host}:{self.port}', file=sys.stderr)

    def stop(self) -> None:
        """Shut the server down cleanly.

        ``self._thread`` is set ONLY by ``start()`` (the daemon-thread path), so
        the thread-join below runs only for servers started that way. For the
        ``serve_forever`` foreground path ``self._thread`` is ``None``: setting
        ``should_exit`` unwinds the blocking ``self._server.run()`` on the main
        thread and the join is skipped (a no-op). ``stop()`` is also idempotent —
        a no-op when ``self._server is None``.
        """
        if self._server is not None:
            self._server.should_exit = True
            if self._thread is not None:
                self._thread.join(timeout=5.0)
            self._server = None
            self._thread = None

    def serve_forever(self, host: str | None = None, port: int | None = None) -> None:
        """Blocking serve — runs uvicorn in the foreground and blocks the caller
        until SIGTERM/SIGINT (or ``stop()`` setting ``should_exit``, which
        unwinds the blocking ``run()`` on this same thread; note ``stop()``'s
        thread-join is a no-op here because ``self._thread`` is only ever set by
        ``start()``).

        A generated env's run.py invokes this as the process's foreground call;
        ``start()`` alone returns immediately and would let the subprocess exit
        before serving any requests.

        ``host`` / ``port`` are optional overrides for the values set at
        construction. Accepting them here makes
        ``service.serve_forever(host=args.host, port=args.port)`` — the Flask-style
        signature a generated run.py is likely to use — work directly.
        """
        if host is not None:
            self.host = host
        if port is not None:
            self.port = port
        config = uvicorn.Config(self._app, host=self.host, port=self.port, log_level='warning')
        self._server = uvicorn.Server(config)
        print(f'[plurai] Mock service at http://{self.host}:{self.port}', file=sys.stderr)
        try:
            self._server.run()
        except KeyboardInterrupt:
            self.stop()
