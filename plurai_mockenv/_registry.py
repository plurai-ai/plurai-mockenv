"""Module-global registries, the session-id ContextVar, and the rebindable
``_client`` slot — the shared mutable state of the mock engine.

CROSS-PACKAGE CONTRACT: the Plurai SDK depends on the shape of this module.
The SDK imports the registry CONTAINERS below (``_mock_handlers``,
``_observer_handlers``, ``_descriptors``, ``_input_schemas``, ``_output_schemas``,
``_DECORATOR_OPT_OUT``) BY REFERENCE — they are only ever mutated IN PLACE
([key]=value, .add(), .items()), never reassigned, so the same dict/set objects
are shared across both packages and cross-package decorator registration works.
The SDK rebinds ``_client`` via QUALIFIED access (``_registry._client = ...``) in
its ``configure()``. Do NOT rebind these containers or rename any name here:
rebinding one (e.g. ``_mock_handlers = {}``) would silently break the SDK's
by-reference binding.
"""

import weakref
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from collections.abc import Callable

if TYPE_CHECKING:
    # MockServiceClient lives in plurai-sdk, NOT here — this package carries no reference
    # to the client class. The SDK rebinds `_client` to a MockServiceClient
    # instance via qualified access at configure() time.
    pass


# The HTTP client slot. None standalone; the SDK rebinds it (NOT mutates it) via
# `_registry._client = MockServiceClient(...)` inside configure(). Annotated as a
# string ref so this package never imports the client class.
_client: 'Any | None' = None
# Sequential callers see this module global; parallel callers (asyncio.gather of
# Orchestrator.run) get isolation via the ContextVar below — each Task's context
# is a copy, so concurrent start_session() calls don't clobber each other.
_module_session_id: str = ''
_session_id_var: ContextVar[str] = ContextVar('plurai_session_id', default='')
_descriptors: dict[str, dict] = {}
_mock_handlers: dict[str, Callable] = {}
_observer_handlers: dict[str, Callable] = {}
# Typed-RPC registries. Populated by @mock
# auto-detect from handler signatures + explicit input_schema/output_schema
# kwargs, and by MockService.register_schemas() from a YAML registry
# emitted by the Plurai backend's environment generator. Only inserts entries with a real Pydantic class —
# absence of a key means "this tool uses the dict path". The wire
# shape stays byte-identical for tools without a registered schema.
_input_schemas: dict[str, type[BaseModel]] = {}
_output_schemas: dict[str, type[BaseModel]] = {}
# Sentinel for "decorator explicitly opted out of typed dispatch on this
# tool". Distinct from "key absent" so register_schemas() can honor an
# explicit @mock(input_schema=None) instead of overwriting it
# with a YAML-supplied schema.
_DECORATOR_OPT_OUT: dict[str, set[str]] = {'input': set(), 'output': set()}

# Services BUILT FROM this staging registry (``build_service``), so a ``@mock`` that runs
# AFTER the build still reaches them. Without it the decorator writes to ``_mock_handlers``,
# the built service holds its own copy taken at build time, and the registration is silently
# dropped — the tool then answers from the unregistered-tool fallback, which is a stub.
#
# WEAK on purpose: a discarded service (every test that builds one) must not be kept alive,
# and a dead one must not receive registrations. Services constructed DIRECTLY with explicit
# ``handlers=`` are deliberately absent — that is the documented "explicit / isolated registry
# set" contract, and pushing to them would violate it.
#
# The engine still reads no module globals: this is the DECORATOR reaching out to the service,
# never the service reaching in.
_staged_services: 'weakref.WeakSet[Any]' = weakref.WeakSet()

# Dedup gate for _resolved_type_hints warnings — keyed by
# (handler-id, exception-class-name). Without this, every per-turn
# dispatch on a broken handler would re-emit the same warning.
_hint_warn_cache: set[tuple[int, str]] = set()


# Sentinel for ``@mock(input_schema=...)``: an explicit ``None``
# means "skip typed dispatch for this direction" (decorator opt-out);
# omitting the kwarg means "fall through to auto-detect". The two
# default-arg paths would collide on ``None`` without a sentinel.
class _NotProvided:
    __slots__ = ()

    def __repr__(self) -> str:
        return '<NotProvided>'


_NOT_PROVIDED = _NotProvided()


def _active_session_id() -> str:
    """Return the per-Task session id when set, falling back to the module global.

    A non-empty ContextVar wins over the module global so two parallel
    ``Orchestrator.run()`` tasks don't see each other's session id. An empty
    ContextVar falls through to the global — that's deliberate: the
    orchestrator's ``finally`` clears its own ContextVar to ``''`` and clears
    the global too, so a sequential caller reading after a clean ``run()``
    sees ``''`` (no leak), while a sibling Task whose ContextVar still holds
    its own session id continues to read that.
    """
    return _session_id_var.get() or _module_session_id


def _set_active_session_id(sid: str) -> None:
    """Update both the per-Task ContextVar and the module global.

    The ContextVar set is what isolates parallel asyncio tasks (each Task has
    its own context copy). The module global is kept in sync so callers
    that read ``_session_id`` from outside any Task still see the latest
    sequentially-set value.
    """
    global _module_session_id
    _module_session_id = sid
    _session_id_var.set(sid)


__all__ = [
    '_DECORATOR_OPT_OUT',
    '_NOT_PROVIDED',
    '_NotProvided',
    '_active_session_id',
    '_client',
    '_descriptors',
    '_hint_warn_cache',
    '_input_schemas',
    '_mock_handlers',
    '_module_session_id',
    '_observer_handlers',
    '_output_schemas',
    '_session_id_var',
    '_set_active_session_id',
]
