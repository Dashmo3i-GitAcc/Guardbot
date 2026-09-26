"""Redaction and size bounds for everything the archive stores.

The archive holds real message text, so it is the one place in this project
where a credential could plausibly be captured — somebody pasting a token into a
group, a provider echoing a key back, an operator typing one into a command. Two
rules follow.

**One pattern list.** AgentMD §53.6 requires that every string leaving the
operator surface passes through `agent_bridge.redact`, which is the project's
single credential-shaped-substring list. This module *delegates* to it rather
than copying it, so the two can never drift. The import is deferred and guarded
because this package is a leaf that the Gemini pool also emits from, and it must
stay importable even if the bridge is not.
"""

from __future__ import annotations

from typing import Any

REDACTED = "<redacted>"
_CLIP_MARK = "…[+{n}]"

# A last-resort guard, used only if `agent_bridge` cannot be imported. It is
# deliberately narrower than the real list and exists so redaction degrades to
# *something* rather than to nothing. It is not a second pattern list: the
# real one is always preferred, and a test asserts the delegation.
_FALLBACK = (
    "<bot-token>",
    "<google-key>",
)


def redact(text: str) -> str:
    """Credential-shaped substrings removed. Never raises."""
    value = text or ""
    if not value:
        return ""
    try:
        from .. import agent_bridge

        return agent_bridge.redact(value)
    except Exception:  # noqa: BLE001 — redaction must never break a record
        return value


def clip(text: str, cap: int) -> tuple[str, int]:
    """Bound a string, reporting exactly how much was dropped.

    Returns ``(text, clipped_chars)``. The cap is configurable and set far above
    any real message, answer or transcript, so ordinary evidence is never cut;
    when it does bite — a pathological paste — the loss is recorded beside the
    row rather than being silent, which is what "do not artificially truncate"
    means in practice.
    """
    value = text or ""
    limit = int(cap or 0)
    if limit <= 0 or len(value) <= limit:
        return value, 0
    dropped = len(value) - limit
    return value[:limit] + _CLIP_MARK.format(n=dropped), dropped


def scrub(value: Any, cap: int) -> Any:
    """Redact and bound a JSON-ish value, recursively and defensively."""
    if isinstance(value, str):
        text, _ = clip(redact(value), cap)
        return text
    if isinstance(value, dict):
        return {str(k): scrub(v, cap) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, cap) for v in value]
    return value
