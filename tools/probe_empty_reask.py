"""Self-cleaning live probe: an unsendable draft is re-asked, not dead-ended.

Boundary under test (2026-09-26). The owner reported, with a timestamp, that the
bot was still answering people with «الان نمیتونم جواب بدم. یه بار دیگه بپرس.» and
«این فایل رو نتونستم باز کنم 🙏…». The log showed the cause was **not** the
throttle sentence: it was `empty_response` — the model came back with an empty
body — in the **chat** workload (`17:45:46`, `17:47:31`) and in the **transcribe**
workload (`17:46:58`), plus one `link_in_reply` (`17:36:58`).

The old behaviour refused such a draft on the **first** try, so a transient empty
answer became a user-visible apology. The fix spends **one bounded re-ask** before
refusing, in both workloads.

This probe drives the **deployed** `chat.reply` and `transcribe.transcribe` with
the network seam stubbed to return a bad draft once and a good answer the second
time — the exact shape of the reported failure — and asserts the answer is
recovered rather than replaced by the apology. It also asserts the boundary is not
weakened: a second bad draft is still refused, and a link is still never sent.

No real model call is made, so the probe is free and deterministic; what it proves
is that the **running image** contains the fix and behaves this way.

Synthetic ids only. Every row it writes is deleted in a finally.

Run it against the **running** container::

    docker cp tools/probe_empty_reask.py guardbot:/tmp/
    docker exec -w /srv -e PYTHONPATH=/srv guardbot python /tmp/probe_empty_reask.py

Or locally from the repo root.
"""
import asyncio
import json
import sqlite3
import sys

from app import chat, config, db, transcribe

CHAT_ID = -1009000000031
USER_ID = 900000301


class Scripted:
    """Stands in for the network: returns each scripted item in turn."""

    def __init__(self, *items):
        self.items = list(items)
        self.calls = 0

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        if not self.items:
            raise AssertionError("more calls than scripted answers")
        return self.items.pop(0)


async def _chat_case(label, script):
    """Drive chat.reply with a scripted seam; return a result dict."""
    stub = Scripted(*script)
    original = chat._request
    chat._request = stub
    try:
        reply = await chat.reply(CHAT_ID, USER_ID, "سلام")
    finally:
        chat._request = original
    return {
        "case": label,
        "answered": bool(reply.answered),
        "error": reply.error,
        "text": reply.text,
        "calls": stub.calls,
    }


async def _transcribe_case(label, script):
    stub = Scripted(*script)
    original = transcribe._request
    transcribe._request = stub
    try:
        result = await transcribe.transcribe(b"audio-bytes", "audio/ogg")
    finally:
        transcribe._request = original
    return {
        "case": label,
        "ok": bool(result.ok),
        "error": result.error,
        "text": result.text,
        "calls": stub.calls,
    }


def _cleanup() -> int:
    """Delete every row this probe wrote. Returns rows deleted."""
    conn = sqlite3.connect(config.DB_PATH)
    try:
        cur = conn.execute(
            "DELETE FROM chat_messages WHERE chat_id = ? AND user_id = ?",
            (CHAT_ID, USER_ID),
        )
        deleted = cur.rowcount
        conn.commit()
        return max(0, deleted)
    finally:
        conn.close()


async def main() -> int:
    db.init()
    # The probe drives several turns in one process, so the deployment's own rate
    # windows would refuse the fourth one — and a refusal is not what is under
    # test here. Raised for the duration; nothing is written back to config.
    config.GEMINI_CHAT_USER_RATE_LIMIT = 1000
    config.GEMINI_CHAT_RATE_LIMIT = 1000
    config.GEMINI_CHAT_DAILY_LIMIT = 100000

    chat_cases = []
    for label, script in (
        # The reported failures: an empty draft, a link draft, and a reply that
        # is only the bare @handle it was answering (which cleans to nothing).
        ("empty_then_good", ["", "سلام! در چه موردی؟"]),
        ("link_then_good", ["برو به t.me/joinchat", "باشه، چشم"]),
        ("handle_only_then_good", ["@Mo3i_ProteCt_Bot", "چیه؟ بگو"]),
        # The boundary must not have been weakened.
        ("empty_then_empty", ["", ""]),
    ):
        chat.reset_state()
        chat_cases.append(await _chat_case(label, script))

    transcribe_cases = []
    for label, script in (
        ("empty_then_good", ["", "سلام به همه"]),
        ("empty_then_empty", ["", ""]),
    ):
        transcribe.reset_state()
        transcribe_cases.append(await _transcribe_case(label, script))

    checks = {
        "empty_recovers": (
            chat_cases[0]["answered"] and chat_cases[0]["calls"] == 2
        ),
        "link_recovers_without_the_link": (
            chat_cases[1]["answered"]
            and "t.me" not in chat_cases[1]["text"]
            and chat_cases[1]["calls"] == 2
        ),
        "handle_only_recovers": (
            chat_cases[2]["answered"] and chat_cases[2]["calls"] == 2
        ),
        "second_empty_still_refused": (
            not chat_cases[3]["answered"]
            and chat_cases[3]["error"] == "empty_response"
            and chat_cases[3]["calls"] == 2
        ),
        "transcribe_empty_recovers": (
            transcribe_cases[0]["ok"] and transcribe_cases[0]["calls"] == 2
        ),
        "transcribe_second_empty_still_fails": (
            not transcribe_cases[1]["ok"]
            and transcribe_cases[1]["error"] == "empty_response"
            and transcribe_cases[1]["calls"] == 2
        ),
    }

    deleted = _cleanup()
    conn = sqlite3.connect(config.DB_PATH)
    try:
        left = conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE chat_id = ?", (CHAT_ID,)
        ).fetchone()[0]
    finally:
        conn.close()

    report = {
        "chat": chat_cases,
        "transcribe": transcribe_cases,
        "checks": checks,
        "all_passed": all(checks.values()),
        "rows_deleted": deleted,
        "rows_left": left,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["all_passed"] and left == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
