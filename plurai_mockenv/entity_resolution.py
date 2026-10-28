"""Resolve an entity id against a world-state map — the one by-id reader every caller shares.

World state is ONE flat map keyed by entity id, with the type stored inside each value as
``_entity_type``. Ids are unique within a type, not across types: a mail thread's id is its
first message's id. When two types share an id, only one of them can hold the bare key (the
other is keyed ``<id>_<n>`` and keeps ``id: <id>``), so a read by key alone returns whichever
type was seeded first. Pass the type you are reading and the answer is always that type's row.
"""

from collections.abc import Mapping
from typing import Any


class AmbiguousEntityId(LookupError):  # noqa: N818 — domain name, matching UnknownSession / InvalidTurn
    """An untyped read of an id that more than one entity type carries.

    Raised instead of returning whichever type holds the bare key: that row is right for one
    reader and silently wrong for the other. The fix is always the typed read,
    ``get_entity(<id>, '<type>')``.
    """

    def __init__(self, entity_id: str, entity_types: list[str]) -> None:
        self.entity_id = entity_id
        self.entity_types = entity_types
        if len(entity_types) == 1:
            message = f'more than one {entity_types[0]!r} row carries entity id {entity_id!r}, and none is keyed by it; no read can pick one'
        else:
            message = (
                f'entity id {entity_id!r} is carried by more than one entity type {entity_types}; '
                f'name the type you are reading: get_entity({entity_id!r}, {entity_types[0]!r})'
            )
        super().__init__(message)


def _carries_id(entity: Any, entity_id: str) -> bool:
    """Does this row's own id — ``id``, or ``<type>_id``, the two keys a seeded row is keyed by — equal ``entity_id``?"""
    if not isinstance(entity, dict):
        return False
    return any(entity.get(f) is not None and str(entity[f]) == entity_id for f in ('id', f'{entity.get("_entity_type")}_id'))


def resolve_entity(state: Mapping[str, Any], entity_id: str, entity_type: str | None = None) -> dict | None:
    """The entity ``entity_id`` names, of ``entity_type`` when given.

    Typed: the key hit when it is of that type, else the one row of that type whose ``id``
    (or ``<type>_id``) field is ``entity_id``; ``None`` when there is none. Several rows of that type carrying the
    id raise :class:`AmbiguousEntityId` rather than picking one.

    Untyped: the key hit, exactly as a plain key read — unless a row of ANOTHER type carries
    the same id, which raises :class:`AmbiguousEntityId`.
    """
    entity_id = str(entity_id)
    hit = state.get(entity_id)
    if entity_type is not None:
        if isinstance(hit, dict) and hit.get('_entity_type') == entity_type:
            return hit
        rows = [v for v in state.values() if isinstance(v, dict) and v.get('_entity_type') == entity_type and _carries_id(v, entity_id)]
        if len(rows) > 1:
            raise AmbiguousEntityId(entity_id, [entity_type])
        return rows[0] if rows else None
    if not isinstance(hit, dict) or hit.get('_entity_type') is None:
        return hit
    others = {v['_entity_type'] for v in state.values() if v is not hit and _carries_id(v, entity_id) and v.get('_entity_type') is not None}
    others.discard(hit['_entity_type'])
    if others:
        raise AmbiguousEntityId(entity_id, [hit['_entity_type'], *sorted(others)])
    return hit
