"""The isolated model seam for automatic memory extraction — **off by default**.

The deterministic layer in ``app/memory.py`` reads statements and counts
behaviour with rules, and it is the whole path for almost every deployment. This
module exists for the narrow remainder the brief allows: a message that plainly
states something durable about its author, in a phrasing no rule anticipated.
For those, and only those, a model may be asked to *structure* the sentence into
one of the closed slots.

It is off unless **both** an operator turns it on (``NEXUS_MEMORY_EXTRACT_MODEL``)
and the isolated ``memory`` workload has its own credential. With either absent,
``enabled()`` is false, ``candidates()`` returns ``[]`` immediately, and no
provider is contacted — which is the state this repository ships in, because no
``GEMINI_MEMORY_API_KEY`` is configured.

The boundaries, and why each is where it is:

**Its own workload, end to end.** The call goes through ``GEMINI_POOLS["memory"]``
— its own key slots, model preference, timeout, retries, backoff, circuit
breaker, daily allowance and counters. A memory backlog can therefore never
spend, delay or exhaust the request somebody is waiting on an answer to, which
is the isolation the brief requires. The module imports nothing from ``chat``,
``awareness``, ``intent``, ``moderation``, ``web_search``, ``transcribe`` or
``voice_live``.

**Its output is untrusted data.** ``candidates()`` returns the model's raw
objects and validates nothing: the trust boundary is ``memory.validate_candidate``
on the server side, which checks the slot against the closed vocabulary and the
value against the same rejections the deterministic path applies. A hallucinated
slot, a link, somebody else's attribute or an oversized value is dropped there,
not here, so there is exactly one place where a candidate becomes a memory.

**It never raises and never blocks.** Every failure — no account, no quota, a
timeout, a 429, a malformed answer — resolves to ``[]``. The caller is a
background task (``memory.observe``) that the answer path never awaits, so the
worst case is that a memory is not learned.

**The key never leaves the environment.** The credential is read by
``app/gemini_pool.py``; nothing here logs, stores or reports it, and a failure
reports only its kind.
"""
from __future__ import annotations

import json
import logging

from . import config, gemini_pool, memory

log = logging.getLogger("guardbot.memory.extract")

# The pool this module uses, and the only one it may use. Named as a constant so
# a test can assert the isolation without reading the call.
WORKLOAD = "memory"

# A hard ceiling on how many candidates one message may contribute, so a model
# that answers with a list of forty cannot turn one message into a write storm.
# The value is small because a single message rarely states more than one or two
# durable things about its author.
MAX_CANDIDATES = 4

_SYSTEM = (
    "You are given one message a person wrote in a Persian Telegram group. "
    "Return the durable, stable facts about THAT PERSON that the message itself "
    "states in the first person — nothing else. Each candidate is a `slot` from "
    "the allowed list and a short `value` copied or lightly normalised from the "
    "message. These are suggestions for a server to check, not decisions.\n"
    "Return an empty list when the message is: about somebody else; a question; "
    "a temporary state (tired, busy, today); a request rather than a statement; "
    "or anything you would have to infer. Never return a mood, a health or "
    "medical detail, a belief, or any psychological trait. Never return more "
    "than a few words per value. When in doubt, return nothing."
)

# A JSON Schema, not a prose request for JSON, so "malformed" means a genuine
# failure rather than a model that decided to write an essay. A plain dict, as
# ``app/ai_intent.py`` uses, so this module imports on a host without the SDK.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slot": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["slot", "value"],
            },
        }
    },
    "required": ["candidates"],
}


def enabled() -> bool:
    """Whether a provider call is permitted at all.

    Two independent conditions, both required: the operator's switch, and an
    account in the isolated pool. Either alone is not enough — the switch without
    a credential must not fall back to the shared chat key (the pool's own
    ``GEMINI_MEMORY_ALLOW_SHARED_KEY`` defaults to false for exactly this
    reason), and a credential without the switch means the feature is not wanted
    yet. Never raises.
    """
    if not config.NEXUS_MEMORY_EXTRACT_MODEL:
        return False
    try:
        return bool(gemini_pool.has_accounts(WORKLOAD))
    except Exception:  # noqa: BLE001 - a gate is never worth a failed handler
        log.exception("could not read the memory pool's accounts")
        return False


def _generation_config(types):
    """The one request shape this workload ever sends."""
    allowed = "; ".join(
        f"{slot} ({memory.SLOTS[slot][1]})" for slot in sorted(memory.SLOTS)
    )
    return types.GenerateContentConfig(
        system_instruction=_SYSTEM + "\nAllowed slots: " + allowed,
        response_mime_type="application/json",
        response_json_schema=RESPONSE_SCHEMA,
        temperature=0.0,
        # A structuring task, not a composition: one small JSON object.
        max_output_tokens=256,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(
            disable=True
        ),
    )


def _parse(raw) -> list[dict]:
    """The model's objects, still untrusted and still unvalidated."""
    try:
        data = json.loads(str(raw or ""))
    except Exception:  # noqa: BLE001 - a malformed answer is simply no answer
        return []
    if not isinstance(data, dict):
        return []
    items = data.get("candidates")
    if not isinstance(items, list):
        return []
    return [item for item in items[:MAX_CANDIDATES] if isinstance(item, dict)]


async def candidates(text: str) -> list[dict]:
    """Ask the isolated workload to structure one message. Never raises.

    Returns the model's raw candidate objects — the server validates them. An
    empty list is the answer to every failure: the switch off, no account, no
    quota, a timeout, a 429, an unusable answer. The caller treats the empty list
    as "nothing learned", which is the same outcome as a message that held no
    memory at all.
    """
    if not enabled():
        return []
    pool = gemini_pool.pool_for(WORKLOAD)
    if pool is None:
        return []
    try:
        raw = await gemini_pool.generate(
            pool,
            build_contents=lambda types: str(text or ""),
            build_config=_generation_config,
        )
    except Exception:  # noqa: BLE001 - memory is never worth a failed handler
        log.exception("memory extraction call failed")
        return []
    return _parse(raw)
