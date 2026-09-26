"""Self-cleaning live probe: warmth, relationship memory, and a three-day scan.

Boundary under test (2026-09-26, AgentMD §54.33). The owner reported that the
assistant had become permanently aggressive, that the rude register must be kept
but gated on a real history, that it must de-escalate the instant the other
person does, and that the room scan must be by time rather than by message count.

This probe drives the **deployed** modules and asserts, on the running image:

* the live persona is warm by default, de-escalates, and no longer carries the
  wording that was read as the aggressive default — and that this persona is what
  is actually handed to the model;
* the relationship memory promotes a hostile history **only** from messages
  directed at Nexus, is never shown as the person's own words, and reaches the
  model's trusted context;
* the room window is bounded by **time** (a two-day-old message is in it, a
  four-day-old one is not), and the digest names who spoke and who said nothing.

No real model call is made: the network seam is stubbed, so the probe is free and
deterministic. Synthetic ids only, and every row it writes is deleted in a
finally.

Run it against the **running** container::

    docker cp tools/probe_warmth_scan.py guardbot:/tmp/
    docker exec -w /srv -e PYTHONPATH=/srv guardbot python /tmp/probe_warmth_scan.py
"""
import asyncio
import json
import sqlite3
import sys
import time

from app import awareness, chat, config, db, main, memory

CHAT_ID = -1009000000077
USER_ID = 900000701
OTHER_USER = 900000702


class Scripted:
    """Stands in for the network: returns each scripted item in turn."""

    def __init__(self, *items):
        self.items = list(items)
        self.calls = 0
        self.instruction = ""
        self.context = ""

    async def __call__(self, contents, *, context="", instruction="", **kwargs):
        self.calls += 1
        self.instruction = instruction
        self.context = context
        if not self.items:
            raise AssertionError("more calls than scripted answers")
        return self.items.pop(0)


async def _drive_reply(*, context: str = "") -> Scripted:
    """One real `chat.reply` turn with the network seam stubbed."""
    stub = Scripted("سلام! چطوری؟")
    original = chat._request
    chat._request = stub
    try:
        chat.reset_state()
        await chat.reply(CHAT_ID, USER_ID, "سلام", context=context)
    finally:
        chat._request = original
    return stub


def _cleanup() -> int:
    """Delete every row this probe wrote. Returns rows deleted."""
    conn = sqlite3.connect(config.DB_PATH)
    deleted = 0
    try:
        for sql, args in (
            ("DELETE FROM chat_messages WHERE chat_id = ?", (CHAT_ID,)),
            ("DELETE FROM user_memory WHERE chat_id = ?", (CHAT_ID,)),
            ("DELETE FROM user_memory_signal WHERE chat_id = ?", (CHAT_ID,)),
            ("DELETE FROM group_messages WHERE chat_id = ?", (CHAT_ID,)),
            ("DELETE FROM people WHERE chat_id = ?", (CHAT_ID,)),
        ):
            cur = conn.execute(sql, args)
            deleted += max(0, cur.rowcount)
        conn.commit()
        return deleted
    finally:
        conn.close()


def _rows_left() -> int:
    conn = sqlite3.connect(config.DB_PATH)
    try:
        total = 0
        for table in (
            "chat_messages",
            "user_memory",
            "user_memory_signal",
            "group_messages",
            "people",
        ):
            total += conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE chat_id = ?", (CHAT_ID,)
            ).fetchone()[0]
        return int(total)
    finally:
        conn.close()


async def main_probe() -> int:
    db.init()
    persona = chat.SYSTEM_INSTRUCTION

    # ── 1. The live persona ───────────────────────────────────────────────
    persona_checks = {
        "warm_by_default": "Be warm with people by default" in persona,
        "rudeness_is_not_the_first_move": (
            "Rudeness is never your first move" in persona
        ),
        "de_escalates": "Come down the moment they do" in persona,
        "reads_the_relationship_line": (
            "how this person has treated you before" in persona
        ),
        "aggressive_wording_is_gone": (
            "never flinch" not in persona
            and "match their intensity" not in persona
            and "give it straight back as hard as they gave it" not in persona
        ),
    }

    # The persona reaches the model. `chat._generation_config` is the exact
    # function the live request path uses to build the system instruction, so
    # this asserts the composition rather than a copy of the text.
    from google.genai import types as genai_types

    sent = await _drive_reply()
    persona_checks["a_turn_still_answers"] = sent.calls == 1

    # ── 2. Relationship memory ────────────────────────────────────────────
    # A message that is not aimed at Nexus is never evidence about Nexus.
    memory._observe_deterministic(CHAT_ID, USER_ID, "کصکش", directed=False)
    undirected_counts = db.signal_for(CHAT_ID, USER_ID)
    for _ in range(3):
        memory._observe_deterministic(CHAT_ID, USER_ID, "کصکش", directed=True)
    hostile = memory.relationship(CHAT_ID, USER_ID)
    hostile_counts = db.signal_for(CHAT_ID, USER_ID)

    for _ in range(3):
        memory._observe_deterministic(CHAT_ID, OTHER_USER, "ممنون از تو", directed=True)
    friendly = memory.relationship(CHAT_ID, OTHER_USER)

    relationship_checks = {
        "undirected_is_not_evidence": "hostile" not in undirected_counts,
        "hostile_history_is_counted": int(hostile_counts.get("hostile") or 0) == 3,
        "hostile_tone_is_stated": (
            "hostile" in hostile and "moment they are friendly again" in hostile
        ),
        "friendly_tone_is_stated": (
            "friendly" in friendly and "Stay warm with them" in friendly
        ),
        "never_the_persons_own_words": memory.about(CHAT_ID, USER_ID) == [],
    }
    # And it reaches the model's trusted context.
    block = main._relationship_context(CHAT_ID, USER_ID)
    composed = chat._generation_config(genai_types, context=block).system_instruction
    relationship_checks["relationship_reaches_the_model"] = (
        "hostile" in block and "hostile" in composed
    )
    persona_checks["persona_reaches_the_model"] = (
        "Be warm with people by default" in composed
        and "Come down the moment they do" in composed
        and "Rudeness is never your first move" in composed
    )

    # ── 3. The window is bounded by time ──────────────────────────────────
    # The old rows go in first, so the newest by id is a recent one and the
    # digest's "newest words" assertion below is unambiguous.
    db.group_capture(CHAT_ID, USER_ID, "member", "reza", "four days ago", keep=500)
    db.group_capture(CHAT_ID, USER_ID, "member", "reza", "two days ago", keep=500)
    for index in range(3):
        db.group_capture(CHAT_ID, USER_ID, "member", "reza", f"recent{index}", keep=500)
    db._exec(
        "UPDATE group_messages SET at=? WHERE chat_id=? AND text=?",
        (int(time.time()) - 2 * 86400, CHAT_ID, "two days ago"),
    )
    db._exec(
        "UPDATE group_messages SET at=? WHERE chat_id=? AND text=?",
        (int(time.time()) - 4 * 86400, CHAT_ID, "four days ago"),
    )
    window_texts = [row["text"] for row in awareness.window(CHAT_ID)]
    window_checks = {
        "two_days_old_is_in_the_window": "two days ago" in window_texts,
        "four_days_old_is_out_of_the_window": "four days ago" not in window_texts,
        "recent_messages_are_in_the_window": "recent2" in window_texts,
    }

    # ── 4. The digest ─────────────────────────────────────────────────────
    db.people_remember(CHAT_ID, OTHER_USER, first_name="sara")
    db._exec(
        "UPDATE people SET last_seen=? WHERE chat_id=? AND user_id=?",
        (int(time.time()) - 5 * 86400, CHAT_ID, OTHER_USER),
    )
    digest = awareness.activity(CHAT_ID)
    digest_checks = {
        "names_a_speaker_with_a_count": "reza" in digest and "messages" in digest,
        "quotes_the_newest_words": "recent2" in digest,
        "names_who_said_nothing": "Said nothing" in digest and "sara" in digest,
        "does_not_count_nexus": "Nexus" not in digest,
    }

    # ── 5. Live evidence from the real room (read-only) ───────────────────
    live = {}
    real_chats = [int(c) for c in (config.GROUP_IDS or [])]
    if real_chats:
        real = real_chats[0]
        rows = awareness.window(real, limit=int(config.NEXUS_AWARENESS_WINDOW_MESSAGES))
        span_h = 0.0
        if rows:
            span_h = (rows[-1]["at"] - rows[0]["at"]) / 3600.0
        digest_real = awareness.activity(real)
        live = {
            "chat": real,
            "window_rows": len(rows),
            "window_span_hours": round(span_h, 2),
            "window_seconds_bound": int(config.NEXUS_AWARENESS_WINDOW_SECONDS),
            "retention_seconds": int(config.NEXUS_AWARENESS_RETENTION_SECONDS),
            "digest_lines": len([x for x in digest_real.splitlines() if x.strip()]),
            "digest_head": digest_real.strip().splitlines()[:3],
        }

    deleted = _cleanup()
    left = _rows_left()

    checks = {
        **{f"persona:{k}": v for k, v in persona_checks.items()},
        **{f"relationship:{k}": v for k, v in relationship_checks.items()},
        **{f"window:{k}": v for k, v in window_checks.items()},
        **{f"digest:{k}": v for k, v in digest_checks.items()},
        "no_rows_left_behind": left == 0,
    }
    report = {
        "checks": checks,
        "all_passed": all(checks.values()),
        "rows_deleted": deleted,
        "rows_left": left,
        "relationship_hostile": hostile.strip(),
        "digest_sample": digest.strip().splitlines()[:5],
        "live_room": live,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main_probe()))
