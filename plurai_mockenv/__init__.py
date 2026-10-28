"""``plurai_mockenv`` — the shared, pydantic-only mock engine + tool descriptor schema.

This package contains exactly the engine the deployed/generated mock env needs:
``MockService`` (the HTTP world-state + event-fold engine), the ``@mock`` /
``@observe`` decorators, the world-state proxies + simulation context, the
typed-RPC helpers, and the ``ToolDescriptor`` descriptor schema. It imports
nothing heavier than stdlib + pydantic — no langchain, no httpx, no
orchestrator. The plurai SDK re-exports this surface; the
deployed env imports it directly so it sheds the SDK's heavy client closure.
"""

from ._engine import MisaddressedWrite
from .builder import build_service, collect_decorated
from .context import (
    EntityStore,
    SimulationContext,
    get_simulation_context,
    world_state,
)
from .decorators import mock, observe
from .dialog import (
    DIALOG_ENTRY_MAX_CHARS,
    DIALOG_MAX_CHARS,
    DIALOG_VERSION,
    PRIOR_CALL_CONTENT_MAX_CHARS,
    PRIOR_CALLS_MAX_CHARS,
    CallContext,
    DialogEntry,
    PriorToolCall,
    SessionDialog,
    cap_dialog,
    cap_prior_calls,
    current_call,
)
from .entity_resolution import AmbiguousEntityId, resolve_entity
from .descriptor_schema import (
    EntityCollectionSatisfies,
    EntityCountChange,
    EntityExistence,
    EntityFieldChange,
    EntityFieldSetter,
    ErrorCase,
    FieldPresence,
    FieldSatisfies,
    PostCondition,
    PreCondition,
    StateFieldChange,
    StateFieldSetter,
    StorageStrategy,
    ToolDescriptor,
)
from .service import MockService
from .world_state import WorldStateProxy, _EntitiesProxy

# Private internals re-exported at the package root because the plurai SDK's
# re-export shim and in-process callers reference them as
# ``plurai_mockenv._hs`` / ``plurai_mockenv._request_local`` /
# ``plurai_mockenv._inside_handler`` / ``plurai_mockenv._set_active_session_id``.
from ._handler_state import _hs, _inside_handler, _request_local  # noqa: F401
from ._registry import _active_session_id, _set_active_session_id  # noqa: F401

__all__ = [
    'DIALOG_ENTRY_MAX_CHARS',
    'DIALOG_MAX_CHARS',
    'DIALOG_VERSION',
    'PRIOR_CALLS_MAX_CHARS',
    'PRIOR_CALL_CONTENT_MAX_CHARS',
    'AmbiguousEntityId',
    'CallContext',
    'DialogEntry',
    'EntityCollectionSatisfies',
    'EntityCountChange',
    'EntityExistence',
    'EntityFieldChange',
    'EntityFieldSetter',
    'EntityStore',
    'ErrorCase',
    'FieldPresence',
    'FieldSatisfies',
    'MisaddressedWrite',
    'MockService',
    'PostCondition',
    'PreCondition',
    'PriorToolCall',
    'SessionDialog',
    'SimulationContext',
    'StateFieldChange',
    'StateFieldSetter',
    'StorageStrategy',
    'ToolDescriptor',
    'WorldStateProxy',
    '_EntitiesProxy',
    'build_service',
    'cap_dialog',
    'cap_prior_calls',
    'collect_decorated',
    'current_call',
    'get_simulation_context',
    'mock',
    'observe',
    'resolve_entity',
    'world_state',
]
