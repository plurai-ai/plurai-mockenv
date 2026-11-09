# plurai-mockenv

The mock engine behind Plurai simulation. It serves the mocked tools an agent calls during a simulated conversation:
you register a plain Python function per tool, and the engine answers the agent's tool calls over HTTP, keeps each
session's world state, and records what the agent did.

It depends only on `pydantic`, `fastapi` and `uvicorn`, so a mock environment runs without the heavier Plurai SDK.

## Install

```bash
pip install plurai-mockenv
```

Python 3.12 and 3.13 are supported.

## Usage

Register a handler for each tool with `@mock`, then build and serve the mock service:

```python
from plurai_mockenv import build_service, mock


@mock('get_order')
def get_order(order_id: str) -> dict:
    """Look up an order by its id."""
    return {'status': 'success', 'content': {'order_id': order_id, 'state': 'shipped'}}


service = build_service(port=8199)
service.serve_forever()
```

The handler's docstring and signature become the tool's description and arguments. A handler whose one parameter is a
Pydantic model gets typed arguments. `@observe` registers an observer for a tool that runs for real: it records the
tool's result in the session's world state.

From the command line, `python -m plurai_mockenv --mocks my_mocks --port 8199` imports the module that holds your
`@mock` handlers and serves them. `python -m plurai_mockenv --help` lists the options.

The main names:

- `build_service(...)` builds a `MockService` from the handlers that `@mock` and `@observe` registered.
- `MockService` is the HTTP service. Construct it directly to pass handlers, observers and descriptors explicitly
  instead of through the decorators.
- `MockEngine` is the engine without HTTP (sessions, tool dispatch and world state), for tests that call it directly.
- `world_state`, `SimulationContext`, `WorldStateProxy` and `EntityStore` read and change a session's world state from
  inside a handler.
- `ToolDescriptor` is the schema of a tool's description.

## With the Plurai SDK

The Plurai SDK (distribution `plurai-simulation-sdk`, import name `plurai`) re-exports these names, so
`plurai.MockService`, `plurai.mock`, `plurai.ToolDescriptor` and `plurai.world_state` are the same objects. The SDK
pins one exact release of `plurai-mockenv`; install the version it pins rather than upgrading this package on its own.

## Issues and release notes

See [CONTRIBUTING.md](https://github.com/plurai-ai/plurai-mockenv/blob/main/CONTRIBUTING.md) for how to report issues,
and [CHANGELOG.md](https://github.com/plurai-ai/plurai-mockenv/blob/main/CHANGELOG.md) for release notes.
