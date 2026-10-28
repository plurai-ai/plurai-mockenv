"""``build_service`` — the canonical declarative constructor for a mock service.

This is the one documented way to stand up a :class:`~plurai_mockenv.service.MockService`
from the ``@mock`` / ``@observe`` decorator staging registry. An env writes its
handlers / observers / typed-RPC schemas with the decorators (which populate a
module-level staging registry) and configures the built-in exposed-fields
projection by passing ``exposed_state_fields`` here — explicit, instance-scoped,
testable.

Registration is HYBRID: the ``@mock`` / ``@observe`` decorators populate a
module-level staging registry (so ``from . import mocks`` import side-effects give
the familiar authoring ergonomics), and the engine reads no module globals.
``build_service`` is the collection point: it imports the ``mocks`` module if asked
(to trigger the decorator side-effects), gathers the staged handlers / observers /
descriptors / typed-RPC schemas / decorator opt-outs, and threads them into the
service explicitly. For full control over the registries — programmatic handlers,
an isolated handler set, or test doubles — construct :class:`MockService`
directly; its constructor takes every registry as an explicit argument.
"""

import importlib
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel

from . import _registry
from .service import MockService


def collect_decorated() -> dict[str, Any]:
    """Snapshot the ``@mock`` / ``@observe`` module-level staging registry.

    Returns a dict of plain copies (never the live containers) shaped for
    ``MockService``: ``handlers``, ``observers``, ``descriptors``, ``schemas``
    (per-tool ``{'input'/'output': Model}``), and ``decorator_opt_out``
    (``{'input'/'output': {tool, ...}}``). Copying here keeps the staging
    registry's container identity intact (the SDK binds it by reference) while
    ensuring the built service never aliases it.
    """
    schemas: dict[str, dict[str, type[BaseModel] | None]] = {}
    for tool_name, in_cls in _registry._input_schemas.items():
        schemas.setdefault(tool_name, {})['input'] = in_cls
    for tool_name, out_cls in _registry._output_schemas.items():
        schemas.setdefault(tool_name, {})['output'] = out_cls
    return {
        'handlers': dict(_registry._mock_handlers),
        'observers': dict(_registry._observer_handlers),
        'descriptors': dict(_registry._descriptors),
        'schemas': schemas,
        'decorator_opt_out': {
            'input': set(_registry._DECORATOR_OPT_OUT['input']),
            'output': set(_registry._DECORATOR_OPT_OUT['output']),
        },
    }


def build_service(
    *,
    host: str = '127.0.0.1',
    port: int = 8199,
    exposed_state_fields: Mapping[str, Iterable[str]] | None = None,
    mocks_module: str | None = None,
) -> MockService:
    """Build a ready-but-unserved :class:`MockService` from the decorator staging registry.

    Order:
      1. Import ``mocks_module`` if given, so its ``@mock`` / ``@observe``
         decorator side-effects populate the staging registry.
      2. Collect handlers / observers / descriptors / typed-RPC schemas /
         decorator opt-outs from the staging registry.
      3. Construct ``MockService`` with everything threaded in explicitly (the
         engine reads no module globals), wiring the built-in
         ``exposed_state_fields`` projection when configured.

    The returned service is NOT serving — call ``.serve_forever()`` (foreground)
    or ``.start()`` (daemon thread). Its instance registries stay mutable: a
    post-build ``register_mock`` / ``register_observer`` / ``register_schemas`` is
    honored at dispatch.

    For an explicit / isolated registry set — programmatic handlers, test doubles,
    or a service that ignores the staging registry — construct :class:`MockService`
    directly: its constructor accepts ``handlers`` / ``observers`` / ``descriptors``
    / ``schemas`` / ``decorator_opt_out``.

    Args:
        host / port: bind address for the served app.
        exposed_state_fields: the ``evaluation`` whitelist driving the built-in
            ``GET /state/{sid}/exposed`` + ``POST /eval/expose`` projection.
        mocks_module: dotted module path to import for decorator side-effects.
    """
    if mocks_module is not None:
        importlib.import_module(mocks_module)

    collected = collect_decorated()
    service = MockService(
        port=port,
        host=host,
        handlers=collected['handlers'],
        observers=collected['observers'],
        descriptors=collected['descriptors'],
        schemas=collected['schemas'],
        decorator_opt_out=collected['decorator_opt_out'],
        exposed_state_fields=exposed_state_fields,
    )
    # Collection above is a SNAPSHOT, and a `@mock` can run after it — a user's simulate.py
    # that defers importing its mocks module (to keep import-smoke cheap) registers everything
    # after `configure(mode='local')` has already built the service. Recording the service here
    # is what lets the decorator deliver those; without it they land in the staging registry
    # that nothing reads, and the tool answers from the unregistered-tool fallback.
    #
    # Only services built FROM the staging registry are recorded. One constructed directly with
    # explicit `handlers=` opted out of it, and must keep its isolated set.
    _registry._staged_services.add(service)
    return service
