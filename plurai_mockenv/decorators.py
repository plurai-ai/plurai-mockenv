"""The ``@mock`` and ``@observe`` decorators."""

import inspect
import logging
from typing import Any

from pydantic import BaseModel

from collections.abc import Callable

from ._handler_state import _hs
from ._registry import (
    _DECORATOR_OPT_OUT,
    _NOT_PROVIDED,
    _descriptors,
    _input_schemas,
    _mock_handlers,
    _observer_handlers,
    _output_schemas,
    _staged_services,
)
from ._typing import _resolved_type_hints, _unwrap_pydantic_class

logger = logging.getLogger(__name__)


def mock(
    tool_name: str,
    descriptor: dict | None = None,
    *,
    input_schema: Any = _NOT_PROVIDED,
    output_schema: Any = _NOT_PROVIDED,
    **desc_kwargs,
):
    """Register dumb inline mock + rich descriptor.

    Auto-populates ``description`` (from the handler docstring or the
    tool name) and ``args`` (from the handler signature) when those keys
    aren't provided explicitly. Both are required by
    ``plurai_mockenv.descriptor_schema.ToolDescriptor`` — keeping the decorator's
    output schema-valid by default means SDK-emitted descriptors round-trip
    cleanly through the backend's mock generator without forcing every
    user to spell out boilerplate.

    Typed-RPC. When the handler's signature has
    exactly one Pydantic-BaseModel-typed parameter (excluding ``self`` /
    ``cls`` / ``ctx``), the wire registers it as the ``input_schema``
    automatically. ``Optional[Model]`` / ``Annotated[Model, ...]`` /
    ``Model | None`` are unwrapped to the underlying class. A Pydantic
    return annotation registers the ``output_schema``.

    Priority: explicit ``input_schema=Foo`` / ``output_schema=Bar`` win
    over auto-detect. Pass ``input_schema=None`` to opt OUT explicitly —
    that disables typed dispatch for this tool/direction AND blocks
    ``MockService.register_schemas()`` from filling it later. Omit
    the kwarg to fall through to auto-detect.
    """

    def decorator(func: Callable) -> Callable:
        desc = descriptor or {}
        desc.update(desc_kwargs)
        if 'description' not in desc:
            docstring = (func.__doc__ or '').strip()
            desc['description'] = docstring.splitlines()[0] if docstring else tool_name
        sig = inspect.signature(func)
        if 'args' not in desc:
            desc['args'] = {n: {'type': str(p.annotation) if p.annotation != inspect.Parameter.empty else 'Any'} for n, p in sig.parameters.items()}
        # Typed-RPC schema registration. Explicit kwargs win (including
        # explicit ``None`` for opt-out); auto-detect runs only when the
        # kwarg was omitted. Use ``_resolved_type_hints`` so PEP 563 /
        # ``from __future__ import annotations`` callers see real
        # classes, not string forward references. ``_unwrap_pydantic_class``
        # peels ``Optional`` / ``Annotated`` / ``Model | None`` so
        # idiomatic Pydantic signatures don't silently fall through to
        # dict.
        explicit_input = input_schema is not _NOT_PROVIDED
        explicit_output = output_schema is not _NOT_PROVIDED
        resolved_input: type[BaseModel] | None = input_schema if explicit_input else None
        resolved_output: type[BaseModel] | None = output_schema if explicit_output else None
        if not explicit_input or not explicit_output:
            hints = _resolved_type_hints(func)
            if not explicit_input:
                candidates: list[type[BaseModel]] = []
                for name in sig.parameters:
                    if name in ('self', 'cls', 'ctx'):
                        continue
                    cls = _unwrap_pydantic_class(hints.get(name))
                    if cls is not None:
                        candidates.append(cls)
                if len(candidates) == 1:
                    resolved_input = candidates[0]
            if not explicit_output:
                resolved_output = _unwrap_pydantic_class(hints.get('return'))
        _mock_handlers[tool_name] = func
        _descriptors[tool_name] = desc
        # Insert only when a real class resolved; explicit-None opts out
        # by recording the tool in the opt-out set so register_schemas
        # won't fill it later.
        if resolved_input is not None:
            _input_schemas[tool_name] = resolved_input
        elif explicit_input:
            _DECORATOR_OPT_OUT['input'].add(tool_name)
        if resolved_output is not None:
            _output_schemas[tool_name] = resolved_output
        elif explicit_output:
            _DECORATOR_OPT_OUT['output'].add(tool_name)
        func._plurai_tool_name = tool_name
        # Deliver to any service ALREADY BUILT from this staging registry. A service takes its
        # own copy of the handlers at build time, so a decorator that runs afterwards would
        # otherwise write only to a letterbox nothing reads — and the tool would answer from
        # the unregistered-tool fallback instead of this handler, silently. Registering with
        # the service is a documented, supported post-build operation (``build_service``:
        # "its instance registries stay mutable").
        #
        # Best-effort by design: a service that has gone away is already dropped from the weak
        # set, and one whose registration raises must not take down an import-time decorator.
        for _service in list(_staged_services):
            try:
                _service.register_mock(tool_name, func, desc)
            except Exception:  # noqa: BLE001 - a decorator must not fail on a service's behalf
                logger.exception('late @mock registration of %r on a live service failed', tool_name)
        return func

    return decorator


def observe(tool_name: str, capture: Callable | None = None):
    """Register observer for real tool → captures result to WorldState."""

    def decorator(func: Callable) -> Callable:
        _observer_handlers[tool_name] = func
        return func

    if capture:

        def auto(args, result):
            entities = capture(args, result)
            if isinstance(entities, dict):
                hs = _hs()
                for eid, edata in entities.items():
                    hs.new_entities[eid] = edata
                    hs.state[eid] = edata

        _observer_handlers[tool_name] = auto
        return lambda f: f
    return decorator
