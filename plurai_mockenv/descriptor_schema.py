"""Pydantic schema for the ``tools.*.descriptor`` block in ``plurai.config.yaml``.

This is the single source of truth for the grammar the Plurai backend's
mock-service generator consumes. The plugin's config verifier
validates generated configs against this; the backend imports
the same types to parse incoming configs. Without a shared schema,
plugin and backend would silently drift on field names.

Strictness: every model sets ``extra="forbid"``. Typos (``change_too``
for ``change``, ``set_too`` for ``set_to``) become validation errors
rather than silently-ignored fields the backend never reads. The
corollary: a key that any consumer of this grammar READS is declared
here even when the engine itself never reads it, because under
``extra="forbid"`` silence is a refusal, not indifference.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# Storage strategies — applied per-direction (read vs write).
#
# ``mocked`` (default): the storage backend is replaced by a faithful,
#   world-state-backed adapter. Reads return what previous writes put there;
#   cross-tool state coherence holds. Requires a corresponding
#   ``stubs.modules`` entry and a monkey-patch in ``simulate.py``.
#
# ``real``: the storage backend stays real. The agent's tool hits the
#   production / test service for this direction. Use for RAG indexes,
#   vector DBs, public reference data, and any read source whose value
#   doesn't need to be controlled by the scenario.
#
# ``stub``: the storage module is substituted via
#   ``sys.modules[...] = MagicMock()`` but no faithful adapter is generated.
#   Use for write-only fire-and-forget targets (analytics, logging,
#   notifications) where the call just needs to not crash.
StorageStrategy = Literal['mocked', 'real', 'stub']


class _Strict(BaseModel):
    """Base for every descriptor type — reject unknown keys."""

    model_config = ConfigDict(extra='forbid')


# ---------------------------------------------------------------------------
# post_conditions variants
# ---------------------------------------------------------------------------


class EntityFieldChange(_Strict):
    """A field on an entity changes by an arithmetic expression.

    Example: ``{entity_type: user, field: balance, change: "-amount"}``.
    The ``change`` value is an expression string evaluated against the
    tool's ``args`` dict at execute time on the backend.
    """

    entity_type: str
    field: str
    change: str


class EntityFieldSetter(_Strict):
    """A field on an entity is set to a literal or an arg-referenced value.

    Example: ``{entity_type: user, field: status, set_to: approved}``.
    """

    entity_type: str
    field: str
    set_to: Any


class EntityCountChange(_Strict):
    """A new entity (or N entities) are created.

    Example: ``{entity_type: transaction, count_change: "+1",
    with_fields: {type: refund, amount: amount}}``. ``count_change`` is an
    arithmetic expression; ``with_fields`` seeds the new entity's fields
    (values may be literals or arg references resolved at execute time).
    """

    entity_type: str
    count_change: str
    with_fields: dict[str, Any] | None = None


class StateFieldSetter(_Strict):
    """A field on the tool's return-value (state) dict is set.

    Example: ``{state_field: status, set_to: approved}``. Distinct from
    ``EntityFieldSetter`` because the target is the response payload, not
    a world-state entity.
    """

    state_field: str
    set_to: Any


class StateFieldChange(_Strict):
    """A field on the tool's return-value (state) dict changes
    non-deterministically.

    Example: ``{state_field: items, change: "len += count"}``.
    Use when the new value can't be expressed as a literal ``set_to``
    (e.g. the field is a list that gets appended, mutated in-place, or
    transformed by an LLM-driven sub-agent). The ``change`` value is
    free-form prose for documentation; the backend mock generator treats
    it as a hint, not an executable expression.

    Distinct from ``EntityFieldChange`` because the target is the
    response payload (state), not a world-state entity.
    """

    state_field: str
    change: str


PostCondition = EntityFieldChange | EntityFieldSetter | EntityCountChange | StateFieldSetter | StateFieldChange


# ---------------------------------------------------------------------------
# pre_conditions variants
# ---------------------------------------------------------------------------


class FieldPresence(_Strict):
    """An arg must be present (and truthy if ``must_be_present: true``).

    Example: ``{field: user_id, must_be_present: true}``.
    """

    field: str
    must_be_present: bool


class FieldSatisfies(_Strict):
    """An arg must satisfy a boolean expression string.

    Example: ``{field: amount, must_satisfy: "amount > 0"}``. The
    expression is evaluated against the ``args`` dict at execute time.
    """

    field: str
    must_satisfy: str


class EntityExistence(_Strict):
    """An entity of the given type must exist at the arg-referenced id.

    Example: ``{entity_type: user, must_exist_with_id: user_id}``. The
    value of ``must_exist_with_id`` is an arg name, not a literal id.
    """

    entity_type: str
    must_exist_with_id: str


class EntityCollectionSatisfies(_Strict):
    """A predicate over a whole collection of entities of a given type.

    Example: ``{entity_type: line_item, must_satisfy: "len > 0"}``.
    Use for "the line_item collection must be non-empty" or
    "there must be at least N line items per order" — predicates over
    entity COLLECTIONS, not over a single arg or a single entity.

    The ``must_satisfy`` value is a boolean expression in prose-or-Python
    form. The backend mock generator uses it to decide whether to admit
    or reject a call. Distinct from ``FieldSatisfies`` because the
    target is an entity collection (queryable via
    ``ctx.world_state.query(entity_type)``), not a function arg.
    """

    entity_type: str
    must_satisfy: str


PreCondition = FieldPresence | FieldSatisfies | EntityExistence | EntityCollectionSatisfies


# ---------------------------------------------------------------------------
# error_cases
# ---------------------------------------------------------------------------


class ErrorCase(_Strict):
    """An error scenario the backend may surface.

    ``condition`` is a prose description ("user not found",
    "insufficient balance") describing when the error fires; ``returns``
    is the response payload override the backend should emit.

    Unlike ``FieldSatisfies.must_satisfy`` (a boolean expression evaluated
    by the backend), ``condition`` is **not evaluated** — it's read by
    the backend's mock generator as documentation/prompt input. The
    generator decides probabilistically or by policy when to fire the
    case and return ``returns`` instead of the nominal response. Don't
    write executable Python here; write a description a human can read.
    """

    condition: str
    returns: dict[str, Any]


# ---------------------------------------------------------------------------
# ToolDescriptor — the top-level descriptor block per tool
# ---------------------------------------------------------------------------


class ToolDescriptor(_Strict):
    """Schema for a single ``tools.<name>.descriptor`` block.

    Required: ``description``. Everything else has a sensible default so
    a minimal read-only tool descriptor can be just ``{description: "..."}``.

    Cross-field invariant: ``writes_to`` non-null implies ``post_conditions``
    non-empty. Without state transitions, the backend mock generator has
    nothing to model the write — it would ship a shape-correct mock that
    leaves world state untouched, breaking multi-turn simulations silently.
    The plugin's config verifier runs an equivalent check as
    defense-in-depth (and surfaces a friendlier ``Finding`` code at the
    plugin layer), but the schema is the source of truth, so backend
    parsing alone is enough to detect the violation.
    """

    description: str
    original_module: str | None = None
    original_function: str | None = None
    reads_from: str | None = None
    writes_to: str | None = None
    # Per-direction storage strategy. Defaults to ``mocked`` when the
    # corresponding ``reads_from`` / ``writes_to`` field is non-null, and
    # ``None`` (irrelevant) otherwise. Explicit ``real`` opts a direction
    # out of the backend's environment-generator adapter-generation / stubs.modules
    # requirements; ``stub`` selects the lighter ``sys.modules`` MagicMock
    # path. See ``StorageStrategy`` for semantics.
    read_strategy: StorageStrategy | None = None
    write_strategy: StorageStrategy | None = None
    stateful: bool = False
    args: dict[str, dict[str, Any]] = Field(default_factory=dict)
    returns: dict[str, Any] | None = None
    agent_usage: str | None = None
    business_rules: list[str] = Field(default_factory=list)
    state_transitions: dict[str, str] = Field(default_factory=dict)
    pre_conditions: list[PreCondition] = Field(default_factory=list)
    post_conditions: list[PostCondition] = Field(default_factory=list)
    error_cases: list[ErrorCase] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    depended_by: list[str] = Field(default_factory=list)
    # Keys the engine itself never reads, declared because the OTHER readers of
    # this grammar do — and ``extra="forbid"`` makes an undeclared key a refusal,
    # not an ignored field. ``role`` is read by the plugin's config verifier
    # (the mocked-identity probe) and by the backend's environment generator;
    # ``input_schema`` by that same generator; ``original_command``,
    # ``http_mapping``, ``original_positional_args`` and ``when`` by the SDK, for
    # the vendored-CLI / HTTP code surfaces that plugin authors write.
    #
    # Deliberately loose (``str | None``, plain containers) rather than Literal
    # or nested models: no engine code path dispatches on any of them, and both
    # ``role`` readers compare exactly and are written to tolerate a value they
    # have no dispatch for. A Literal would make this schema the arbiter of a
    # vocabulary it never reads and would refuse, at the plugin's schema probe,
    # the very config those readers accept. Contrast ``read_strategy``, which is
    # a Literal because ``effective_read_strategy()`` below dispatches on it.
    role: str | None = None
    input_schema: dict[str, Any] | None = None
    original_command: str | None = None
    http_mapping: dict[str, Any] | None = None
    original_positional_args: list[Any] = Field(default_factory=list)
    when: str | None = None

    def effective_read_strategy(self) -> StorageStrategy | None:
        """Resolve ``read_strategy`` against the default (``mocked`` when
        ``reads_from`` is non-null, ``None`` otherwise)."""
        if self.read_strategy is not None:
            return self.read_strategy
        if isinstance(self.reads_from, str) and self.reads_from.strip():
            return 'mocked'
        return None

    def effective_write_strategy(self) -> StorageStrategy | None:
        """Resolve ``write_strategy`` against the default (``mocked`` when
        ``writes_to`` is non-null, ``None`` otherwise)."""
        if self.write_strategy is not None:
            return self.write_strategy
        if isinstance(self.writes_to, str) and self.writes_to.strip() and self.writes_to.strip().lower() not in {'none', 'null'}:
            return 'mocked'
        return None

    @model_validator(mode='after')
    def _writes_to_requires_post_conditions(self) -> ToolDescriptor:
        # writes_to is "non-null" when it's a non-empty string that isn't
        # one of YAML's null-ish spellings ("null", "none", "" — the LLM
        # sometimes emits these as the literal string instead of YAML null).
        # Matches the heuristic of the plugin's config verifier.
        writes_to = self.writes_to
        if writes_to is None:
            return self
        if isinstance(writes_to, str) and writes_to.strip().lower() in {'', 'none', 'null'}:
            return self
        # post_conditions are only required when the backend will actually
        # MODEL the write — i.e. effective_write_strategy is "mocked". For
        # stub-strategy writes (fire-and-forget logging, analytics) the
        # backend never generates an adapter, so there's nothing for
        # post_conditions to drive. For real-strategy writes (writes go to
        # a real backend) the user is intentionally outside the
        # mock-coherence guarantee.
        if self.effective_write_strategy() != 'mocked':
            return self
        if not self.post_conditions:
            raise ValueError(
                'writes_to is non-null and write_strategy is mocked '
                '(default) but post_conditions is empty. Add at least one '
                'entry describing the state change (e.g., '
                "{entity_type: X, count_change: '+1'}); the backend mock "
                'generator needs structured state transitions to model '
                'the write. (Or set write_strategy: stub / real if the '
                "write shouldn't be faithfully mocked.)"
            )
        return self


__all__ = [
    'EntityCollectionSatisfies',
    'EntityCountChange',
    'EntityExistence',
    'EntityFieldChange',
    'EntityFieldSetter',
    'ErrorCase',
    'FieldPresence',
    'FieldSatisfies',
    'PostCondition',
    'PreCondition',
    'StateFieldChange',
    'StateFieldSetter',
    'StorageStrategy',
    'ToolDescriptor',
]
