# plurai-mockenv

The shared, **pydantic-only** mock engine and tool descriptor schema that powers
Plurai's deployed/generated mock environments.

`plurai-mockenv` contains exactly the engine a deployed mock env needs and nothing
heavier:

- **`MockEngine`** — the deep domain engine: session store, event-fold,
  `/mock/execute` + `/mock/observe` dispatch, typed-RPC, and the inside-handler
  bypass. Speaks domain types and raises domain errors (`UnknownSession`,
  `InvalidTurn`); knows nothing about HTTP. It is the test surface
  (`engine.execute(...)` directly).
- **`MockService`** — the transport adapter: a **FastAPI app served by uvicorn**
  that owns the frozen wire contract (`/session/create`, `/session/turn`,
  `/mock/execute`, `/mock/observe`, `/state/*`, `/events/*`, `/health`). `_handle` is the
  engine's internal FastAPI funnel dispatcher — it is **not** a public override
  hook. Handlers / observers / typed-RPC schemas are registered with the `@mock`
  / `@observe` decorators; the declarative `build_service(...)` factory collects
  that staging registry and wires the built-in exposed-projection-v1 routes
  (`GET /state/{sid}/exposed`, `POST /eval/expose`) when you pass
  `exposed_state_fields`. The canonical entrypoints are `build_service(...)`
  (programmatic) and `python -m plurai_mockenv` (CLI). For an explicit / isolated
  registry set, construct `MockService(...)` directly (its constructor takes
  `handlers` / `observers` / `descriptors` / `schemas` / `decorator_opt_out`).
- **`@mock` / `@observe`** — the decorators that register inline mock handlers and
  observers + their descriptors.
- **`SimulationContext` / `WorldStateProxy` / `EntityStore` / `world_state`** — the
  world-state proxies and the module-level `world_state` singleton, with the
  inside-handler direct-memory bypass vs. outside-handler HTTP path.
- **`ToolDescriptor`** and its condition models (the `descriptor_schema` surface) —
  the single source of truth for the `tools.*.descriptor` grammar the backend's
  mock generator consumes.

## Install

```bash
pip install plurai-mockenv
```

Supported Python versions: 3.12 and 3.13. Consumers that import the private hook surface, such as the
Plurai SDK (`plurai-simulation-sdk`), pin this package to an exact version (`plurai-mockenv==X.Y.Z`) and
bump it per release; that surface is not part of the public API.

## Purity invariant

`plurai_mockenv` imports nothing heavier than the standard library + `pydantic` +
`fastapi`/`uvicorn` (the transport adapter). **No langchain, no httpx, no
orchestrator** in the runtime closure — fastapi/uvicorn/starlette pull neither
langchain nor httpx. `httpx` appears only as a DEV/test dependency (FastAPI's
`TestClient`), never at runtime. This is the whole point: it lets a deployed mock
env shed the heavy `plurai` SDK client closure (langchain-core + httpx +
orchestrator + observability). The property: in a fresh environment that holds only
`plurai-mockenv`, `import plurai_mockenv` succeeds while `import langchain_core` and
`import httpx` both fail.

## Consumers

- **`plurai` (the client SDK)** re-exports this surface, so `plurai.MockService`, `plurai.mock`,
  `plurai.ToolDescriptor` and `plurai.world_state` are the same objects.
- **Generated mock envs** `import plurai_mockenv` directly (and run under a host venv
  that carries only the pydantic-only engine), so they never install the SDK.

The `/mock/*` JSON wire shapes are the contract; they are frozen between the
SDK and this package.
