"""The session dialog and the per-call context a handler reads.

The harness pushes the conversation so far on ``POST /session/turn``; the engine caps it with
:func:`cap_dialog` and stores it on the session. For every ``/mock/execute`` the engine builds a
frozen :class:`CallContext` — the stored dialog, the mocked tool calls of earlier turns taken from
the session's own event log, and the ``call_seq`` that seeds deterministic world ids — and a
handler reads it through :func:`current_call`.

The dialog is context for interpreting a request, never a source of facts. This module only
carries it; what may be cited is decided by the runtime that reads it.

The caps are character counts, so the result is deterministic and needs no tokenizer. The SDK
applies them before sending and the engine applies them again on store; the engine's is the one
that counts, because callers other than the SDK reach the route too.

``current_call()`` is per THREAD: it answers only on the thread that runs the handler. A thread the
handler spawns (an executor enforcing a deadline, say) sees ``None``, and ``world_state.generate_uuid()``
there falls back to a random id — outside replay. Read ``current_call()`` on the handler thread and pass
the context (and any ids) to helper threads explicitly.
"""

import hashlib
import json
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel

from ._handler_state import _hs, _inside_handler

DIALOG_VERSION: Literal[1] = 1
DIALOG_ENTRY_MAX_CHARS = 4_000
DIALOG_MAX_CHARS = 24_000
PRIOR_CALLS_MAX_CHARS = 8_000
PRIOR_CALL_CONTENT_MAX_CHARS = 1_000

TRUNCATION_MARKER = '…[truncated]'


def canonical_json(value: Any) -> str:
    """The one serialisation every cap and digest here measures. Never raises on a handler's odd value:
    a non-JSON value is rendered with ``str``, and keys of mixed types keep their insertion order."""
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str)
    except TypeError:
        return json.dumps(value, separators=(',', ':'), ensure_ascii=False, default=str)


def truncate_text(text: str, limit: int) -> str:
    """``text`` unchanged when it fits in ``limit`` chars; else its head plus the marker, ``limit`` chars in all."""
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


class DialogEntry(BaseModel, extra='forbid', frozen=True):
    turn: int
    role: Literal['user', 'agent']
    text: str


class SessionDialog(BaseModel, extra='forbid'):
    version: Literal[1] = 1
    entries: list[DialogEntry] = []
    dropped_entries: int = 0


def cap_dialog(entries: Sequence[DialogEntry]) -> SessionDialog:
    """Each entry truncated to ``DIALOG_ENTRY_MAX_CHARS``; then the OLDEST whole entries dropped until
    the texts fit in ``DIALOG_MAX_CHARS``, counted in ``dropped_entries``."""
    capped = [e if len(e.text) <= DIALOG_ENTRY_MAX_CHARS else e.model_copy(update={'text': truncate_text(e.text, DIALOG_ENTRY_MAX_CHARS)}) for e in entries]
    total = sum(len(e.text) for e in capped)
    dropped = 0
    while total > DIALOG_MAX_CHARS:
        total -= len(capped[dropped].text)
        dropped += 1
    return SessionDialog(entries=capped[dropped:], dropped_entries=dropped)


class PriorToolCall(BaseModel, frozen=True):
    turn: int
    tool: str
    args: dict[str, Any]
    content: Any  # capped to PRIOR_CALL_CONTENT_MAX_CHARS


_MIN_ARG_CHARS = 2 * len(TRUNCATION_MARKER)


def _cap_content(content: Any, limit: int = PRIOR_CALL_CONTENT_MAX_CHARS) -> Any:
    text = content if isinstance(content, str) else canonical_json(content)
    if len(text) > limit:
        return truncate_text(text, limit)
    return content if isinstance(content, str) else json.loads(text)


def _cap_args(args: dict[str, Any]) -> dict[str, Any]:
    """Args within ``PRIOR_CALL_CONTENT_MAX_CHARS`` of canonical JSON unchanged; else each value capped to
    an equal share of it, so one oversized call cannot crowd every other call out of the window."""
    normal = json.loads(canonical_json(args))
    if len(canonical_json(normal)) <= PRIOR_CALL_CONTENT_MAX_CHARS:
        return normal
    share = max(PRIOR_CALL_CONTENT_MAX_CHARS // max(len(normal), 1), _MIN_ARG_CHARS)
    return {k: _cap_content(v, share) for k, v in normal.items()}


def cap_prior_calls(calls: Sequence[PriorToolCall]) -> list[PriorToolCall]:
    """Each call's ``content`` capped to ``PRIOR_CALL_CONTENT_MAX_CHARS`` (a longer value becomes its
    truncated JSON text) and its ``args`` to the same size; then the NEWEST whole calls kept within ``PRIOR_CALLS_MAX_CHARS`` of
    canonical JSON, returned in the order given. Mocked and live calls are capped alike."""
    kept: list[PriorToolCall] = []
    budget = PRIOR_CALLS_MAX_CHARS
    for call in reversed(calls):
        capped = PriorToolCall(turn=call.turn, tool=call.tool, args=_cap_args(call.args), content=_cap_content(call.content))
        size = len(canonical_json(capped.model_dump(mode='json')))
        if size > budget:
            break
        budget -= size
        kept.append(capped)
    kept.reverse()
    return kept


class CallContext(BaseModel, frozen=True):
    session_id: str
    turn: int
    call_seq: int
    initial_digest: str
    dialog: SessionDialog
    prior_tool_calls: list[PriorToolCall]

    def digest(self) -> str:
        """sha256 over the dialog and the prior tool calls ONLY — never the session id, turn or call_seq,
        so the same call repeated within a turn, or replayed in another session, digests the same."""
        payload = {'dialog': self.dialog.model_dump(mode='json'), 'prior_tool_calls': [c.model_dump(mode='json') for c in self.prior_tool_calls]}
        return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def current_call() -> CallContext | None:
    """The context of the ``/mock/execute`` call the running handler serves; ``None`` outside a handler."""
    return _hs().call if _inside_handler() else None
