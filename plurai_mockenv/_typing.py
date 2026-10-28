"""Typed-RPC helpers: resolve handler type hints + unwrap Pydantic classes."""

import logging
from typing import Any, get_args, get_origin, get_type_hints

from pydantic import BaseModel

from collections.abc import Callable

from ._registry import _hint_warn_cache

logger = logging.getLogger(__name__)


def _resolved_type_hints(func: Callable) -> dict[str, Any]:
    """Resolve a handler's annotations through ``typing.get_type_hints`` so
    PEP 563 / ``from __future__ import annotations`` callers see real
    classes, not the string forward references that
    ``inspect.signature(func).parameters[name].annotation`` yields.

    Returns ``{}`` and logs one warning per (func, exc-type) when
    resolution fails (typically ``NameError`` from a forward ref that
    isn't in the function's globals, ``ImportError`` from a circular
    import, or ``TypeError`` from a C-function lacking
    ``__annotations__``). The broad catch is deliberate: typed-RPC must
    degrade silently to the dict path; the dedup'd warning is the
    operator-visible drift signal.
    """
    try:
        return get_type_hints(func)
    except Exception as exc:  # noqa: BLE001 — narrowing risks losing drift signal
        key = (id(func), type(exc).__name__)
        if key not in _hint_warn_cache:
            _hint_warn_cache.add(key)
            logger.warning(
                'typed-RPC hint resolution failed for %s: %s: %s (falling back to dict path)',
                getattr(func, '__qualname__', func),
                type(exc).__name__,
                exc,
            )
        return {}


def _unwrap_pydantic_class(hint: Any) -> type[BaseModel] | None:
    """Return the Pydantic ``BaseModel`` subclass represented by ``hint``,
    or ``None`` if the hint isn't one (or contains a union with multiple
    Pydantic types). Unwraps ``Optional[Model]``, ``Union[Model, None]``,
    PEP 604 ``Model | None``, and ``Annotated[Model, ...]`` — the
    auto-detect heuristic should fire for those, not silently fall
    through to dict.
    """
    if isinstance(hint, type) and issubclass(hint, BaseModel):
        return hint
    origin = get_origin(hint)
    if origin is None:
        return None
    args = [a for a in get_args(hint) if a is not type(None)]
    pydantic_args = [a for a in args if isinstance(a, type) and issubclass(a, BaseModel)]
    # If recursion finds a Pydantic class through Annotated[...] or a
    # single-class Union, lift it. Multiple Pydantic classes in a union
    # are ambiguous — fall through to dict so the user can disambiguate
    # via an explicit input_schema=.
    if len(pydantic_args) == 1 and len(args) == 1:
        return pydantic_args[0]
    # Annotated[Model, ...] surfaces the Model as the first arg even when
    # there are metadata args — recurse on it.
    if args:
        return _unwrap_pydantic_class(args[0])
    return None
