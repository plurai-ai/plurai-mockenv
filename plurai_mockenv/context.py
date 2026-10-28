"""SimulationContext + the module-level ``world_state`` singleton + EntityStore."""

from . import _registry
from ._handler_state import _hs, _inside_handler
from ._registry import _active_session_id
from .world_state import WorldStateProxy


class SimulationContext:
    """Available everywhere: @mock handlers, replacement nodes, user code."""

    def __init__(self):
        self.world_state = WorldStateProxy()

    @property
    def agent_context(self) -> dict:
        if _inside_handler():
            return _hs().context
        return _registry._client.get_context(_active_session_id()) if _registry._client else {}


_ctx = SimulationContext()

# Module-level alias so a user's storage classes can write
# ``world_state.apply(...)`` symmetrically with ``ctx.world_state``
# inside @mock handlers. Same WorldStateProxy instance either way —
# the proxy already gates inside-handler (direct memory) vs outside
# (HTTP) on its own, so no caller cares which entry point it used.
world_state = _ctx.world_state


def get_simulation_context() -> SimulationContext:
    return _ctx


class EntityStore:
    """Thin wrapper. Reads from server via SimulationContext."""

    def __init__(self, world_state_data: dict = None):
        pass  # Not needed — use get_simulation_context().world_state

    def get(self, entity_id: str, entity_type: str | None = None) -> dict | None:
        return _ctx.world_state.get_entity(entity_id, entity_type)

    def query(self, entity_type: str, **filters) -> list[dict]:
        return _ctx.world_state.query(entity_type, **filters)
