"""Per-request handler bypass state — thread-local, one ``_HandlerState`` per thread."""

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .dialog import CallContext


class _HandlerState:
    """Per-request bypass state. One instance per thread (via ``_hs()``).

    Fields are populated by ``MockService._handle`` before the
    user's mock/observe handler runs and read back after it returns
    to capture the state delta + new-entity dict. Initialized to empty
    dicts in ``__init__`` so each thread gets independent storage rather
    than aliasing a shared class-level default.
    """

    __slots__ = ('state', 'context', 'updates', 'new_entities', 'refused', 'call', 'minted')

    def __init__(self) -> None:
        self.state: dict = {}
        self.context: dict = {}
        self.updates: dict = {}
        self.new_entities: dict = {}
        #: The first world write this call refused (``MisaddressedWrite``), even if the handler caught it.
        self.refused: str | None = None
        #: The ``/mock/execute`` call this handler serves (``current_call()``); ``None`` outside one.
        self.call: CallContext | None = None
        #: How many ids ``generate_uuid`` has minted in this call — the ``n`` of its seed.
        self.minted: int = 0


_request_local = threading.local()


def _inside_handler() -> bool:
    """True when the calling thread is currently executing a user mock
    or observe handler inside the mock service's request dispatch.

    The proxy types (WorldStateProxy, _EntitiesProxy) gate on this to choose
    between the direct-memory bypass and the HTTP-client path.
    """
    return getattr(_request_local, 'inside', False)


def _hs() -> _HandlerState:
    """Per-thread handler-state accessor; lazily creates the slot on first use."""
    state = getattr(_request_local, 'hs', None)
    if state is None:
        state = _HandlerState()
        _request_local.hs = state
    return state
