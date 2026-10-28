"""WorldState proxies — HTTP-backed outside handlers, direct-memory inside them."""

import hashlib
import uuid
from collections.abc import MutableMapping

from . import _registry
from ._engine import MisaddressedWrite, refuse_misaddressed
from .dialog import current_call
from ._handler_state import _HandlerState, _hs, _inside_handler
from ._registry import _active_session_id
from .entity_resolution import resolve_entity


def _refuse_in_handler(hs: _HandlerState, updates: dict) -> None:
    """Refuse a misaddressed write before it mutates, and remember it so ``execute`` voids the call even if the handler catches it."""
    try:
        refuse_misaddressed(hs.state, updates)
    except MisaddressedWrite as exc:
        if hs.refused is None:
            hs.refused = str(exc)
        raise


def _raise_if_refused(result: object) -> None:
    """The service's ``write_refused`` answer, raised as the refusal it is."""
    if isinstance(result, dict) and result.get('status') == 'write_refused':
        raise MisaddressedWrite(result.get('error', 'world write refused'))


class WorldStateProxy:
    """
    Reads/writes WorldState through the mock service.

    Inside @mock handler (local server): uses direct memory (avoids deadlock).
    Everywhere else (replacement nodes, user code): uses HTTP to server.
    """

    def get_entity(self, entity_id: str, entity_type: str | None = None) -> dict | None:
        """The entity ``entity_id`` names — of ``entity_type`` when given (see :func:`resolve_entity`).

        Ids are unique within a type, not across types, so pass the type you are reading: an
        untyped read of an id two types share raises ``AmbiguousEntityId``.
        """
        if _inside_handler():
            return resolve_entity(_hs().state, entity_id, entity_type)
        if _registry._client is None:
            return None
        if entity_type is None:
            return _registry._client.get_entity(_active_session_id(), entity_id)
        return resolve_entity(_registry._client.get_full_state(_active_session_id()).get('state', {}) or {}, entity_id, entity_type)

    def apply(self, updates: dict[str, dict]):
        if _inside_handler():
            hs = _hs()
            _refuse_in_handler(hs, updates)
            for eid, fields in updates.items():
                if eid not in hs.updates:
                    hs.updates[eid] = {}
                hs.updates[eid].update(fields)
                if eid not in hs.state:
                    hs.state[eid] = {}
                hs.state[eid].update(fields)
            return
        # Outside-handler path: a user's storage classes and ``seed_state`` write
        # through this method. The underlying
        # ``_client.update_state`` returns ``{"error": ...}`` on HTTP
        # failure rather than raising, which would leave a partial seed
        # with no exception (a mock service that stops mid-init, an agent
        # that then runs against an incomplete world). Surface
        # the failure so callers can decide whether to retry / abort /
        # tolerate, instead of silently corrupting world state.
        if _registry._client is None:
            raise RuntimeError(
                'plurai.world_state.apply called before plurai.configure(). '
                'Inside the child subprocess, configure runs in handle_init '
                'before seed_state; outside the subprocess, call '
                'plurai.configure(service_url=...) explicitly.'
            )
        result = _registry._client.update_state(_active_session_id(), updates)
        _raise_if_refused(result)
        if isinstance(result, dict) and 'error' in result:
            raise RuntimeError(
                f'plurai.world_state.apply failed: {result["error"]}. The '
                'mock service rejected or could not receive the update — '
                'subsequent reads will see stale/empty state.'
            )

    def query(self, entity_type: str, **filters) -> list[dict]:
        if _inside_handler():
            return [
                e
                for e in _hs().state.values()
                if isinstance(e, dict) and e.get('_entity_type') == entity_type and all(e.get(k) == v for k, v in filters.items())
            ]
        return _registry._client.query_state(_active_session_id(), entity_type, **filters) if _registry._client else []

    def generate_uuid(self) -> str:
        """A new 8-hex id. Inside a handler it replays: ``sha256(f'{initial_digest}:{call_seq}:{n}')[:8]``
        for the n-th id this call mints, skipping a token that any key the world holds already contains —
        so the same initial world and the same call order mint the same ids in any session or process.
        Outside a handler (harness code, a helper thread: out of replay scope) it is random."""
        call = current_call()
        if call is None:
            return str(uuid.uuid4())[:8]
        hs = _hs()
        while True:
            n, hs.minted = hs.minted, hs.minted + 1
            candidate = hashlib.sha256(f'{call.initial_digest}:{call.call_seq}:{n}'.encode()).hexdigest()[:8]
            # A key that merely CONTAINS the token (``row-<id>``, ``<type>_<id>``) holds it too.
            if not any(candidate in str(key) for key in hs.state):
                return candidate

    @property
    def entities(self):
        return _EntitiesProxy()


class _EntitiesProxy(MutableMapping):
    """Mapping-like view into the active scenario's entities.

    Subclasses :class:`collections.abc.MutableMapping` so the standard
    dict-iteration contract (``keys``/``values``/``items``/``__iter__``/``in``)
    works as callers expect. A class exposing only ``keys()`` (no
    ``values``/``items``/``__iter__``) would silently break any code that
    treats ``world_state.entities`` like a dict — including the canonical
    ``isinstance(..., dict)`` check, which the backend verifier rejects.

    Inside ``@mock`` handlers (``_inside_handler() is True``): reads
    and iteration hit the thread-local ``_hs().state`` directly — zero HTTP
    cost. Outside handlers: each iteration calls ``_client.get_full_state``
    once; each ``__getitem__`` calls ``_client.get_entity`` once. For bulk
    reads outside handlers, prefer :func:`plurai.world_state_snapshot` —
    a single explicit HTTP call.

    ``__getitem__`` is **strict** and raises ``KeyError`` on missing keys
    (matches the Mapping contract; surfaces typos instead of returning an
    empty dict that hides them). Callers wanting a default should use
    ``entities.get(eid, default)``.

    ``__delitem__`` raises ``NotImplementedError`` — entity deletion isn't
    defined in the event-folded state model used by the mock service.
    """

    def _snapshot(self) -> dict[str, dict]:
        """Single source of truth for the proxy's read path.

        Inside a handler: returns the per-thread state view directly.
        Outside a handler: fetches the full state via one HTTP call.
        """
        if _inside_handler():
            return _hs().state
        if _registry._client is None:
            return {}
        return _registry._client.get_full_state(_active_session_id()).get('state', {}) or {}

    def __getitem__(self, key: str) -> dict:
        if _inside_handler():
            state = _hs().state
            if key not in state:
                raise KeyError(key)
            return state[key]
        if _registry._client is None:
            raise KeyError(key)
        entity = _registry._client.get_entity(_active_session_id(), key)
        if entity is None:
            raise KeyError(key)
        return entity

    def __setitem__(self, key: str, value: dict) -> None:
        if _inside_handler():
            hs = _hs()
            _refuse_in_handler(hs, {key: value})
            hs.new_entities[key] = value
            hs.state[key] = value
        elif _registry._client is not None:
            _raise_if_refused(_registry._client.update_state(_active_session_id(), {key: value}))

    def __delitem__(self, key: str) -> None:
        raise NotImplementedError("Entity deletion via the proxy isn't supported. Mutate the entity to a tombstone state via world_state.apply(...) instead.")

    def __iter__(self):
        return iter(self._snapshot())

    def __len__(self) -> int:
        return len(self._snapshot())

    # --- Bulk views: ONE snapshot, not one fetch per key -----------------------------------
    #
    # ``Mapping`` synthesises ``items()``/``values()`` as ``self[k] for k in self``, which on
    # this proxy is ``__iter__`` (one snapshot) followed by ``__getitem__`` per key (one HTTP
    # GET each) — N+1 round-trips for one bulk read.
    #
    # The synthesised versions are also LESS consistent, not more: the keys come from one
    # snapshot and each value from a separate, later read, so a moving world yields a torn
    # pairing that never existed at any instant. Reading both from one snapshot is cheaper AND
    # atomic. ``__getitem__`` keeps its per-key fetch — a single-key read should be fresh; it
    # is bulk iteration that has no business paying per key.
    #
    # Materialised via ``dict(...)`` so the view is stable while the caller iterates: inside a
    # handler ``_snapshot()`` hands back the live thread-local state, which a handler writing
    # through ``world_state.apply`` can mutate mid-loop.

    def items(self):
        return dict(self._snapshot()).items()

    def values(self):
        return dict(self._snapshot()).values()
