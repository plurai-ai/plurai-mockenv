# Changelog

## 1.5.5

First public release of `plurai-mockenv`: the pydantic-only mock engine and tool descriptor schema behind Plurai simulation environments.

- **Mock engine.** The engine keeps sessions, folds each session's event log into its world state, and dispatches mocked tool calls and observations. `MockService` serves it over HTTP (FastAPI and uvicorn): `/session/*`, `/mock/execute`, `/mock/observe`, `/state/*`, `/events/*` and `/health`, plus `/eval/expose` when `exposed_state_fields` is set. `build_service(...)` builds a service from the registered handlers, and `python -m plurai_mockenv` builds and serves one.
- **`@mock` and `@observe`.** `@mock` registers an inline tool handler together with its descriptor; `@observe` registers an observer that captures the result of a real tool call into the world state. A `@mock` handler registered after `build_service(...)` has returned still reaches that service. A tool without a handler answers with an explicit error, not a result.
- **`ToolDescriptor`.** A Pydantic schema, strict about unknown keys, for the `tools.*.descriptor` block of a Plurai config: pre- and post-conditions, error cases and per-direction storage strategies.
- **World state.** `world_state` and `SimulationContext` read and write entities, directly in memory inside a handler and over HTTP elsewhere through the client that the Plurai SDK configures. A write without an `_entity_type` to a key that no entity holds is refused with `MisaddressedWrite`, as is a write that declares an `_entity_type` onto a key held by another type. `world_state.generate_uuid()` mints replayable ids inside a handler. A handler reads the session dialog and the tool calls of earlier turns through `current_call()`.
- **Typed RPC.** A handler that annotates a parameter or its return value with a Pydantic model, or passes `input_schema` / `output_schema`, receives validated arguments, and a Pydantic response is recorded as JSON. Input that fails validation falls back to the dict path, and the recorded event says so.
- **Supported Python:** 3.12 and 3.13.
- **Pinning.** Consumers that import the private hook surface, such as the Plurai SDK, pin an exact version (`plurai-mockenv==X.Y.Z`); that surface is not part of the public API.
- **Licence.** No licence is declared.
