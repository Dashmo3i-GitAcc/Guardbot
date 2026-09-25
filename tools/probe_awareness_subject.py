"""Self-cleaning live probe of Nexus's self-awareness, in-container.

Boundary under test (2026-09-25): the Awareness pass must understand that a room
is talking **about** Nexus even when nobody writes its name and nobody replies to
it, must stay quiet when the evidence is weak or the subject is something else,
and must attach its answer to the message it is actually answering.

For each scenario the probe does two real passes through ``main._awareness_pass``:

* the first records the server's own subject reading (the model is stubbed and
  declines), which is what the deterministic layer contributes; and
* the second hands the pass a confident decision, which is what shows whether the
  participation floor lets it speak and which message the reply quotes.

The model seam is replaced, so no scenario costs a request except the single
optional ``--real`` turn. The send seam records the ``reply_to_message_id`` the
bot was actually handed, so the Telegram destination is asserted rather than
inferred. Synthetic room and user ids only; everything created is deleted in a
finally and the count left behind is printed.

Run it against the **running** container (WORKDIR is ``/srv``)::

    docker cp tools/probe_awareness_subject.py guardbot:/tmp/
    docker exec -w /srv -e PYTHONPATH=/srv guardbot python /tmp/probe_awareness_subject.py

Add ``--real`` for one real-model pass. Or locally from the repo root.
"""
import asyncio
import json
import sys
import time

from telegram import Chat, User
from telegram.constants import ChatType

from app import awareness, chat, config, db, groups, nexus, rbac
from app import main as m

ROOM = -1009000000031          # registered by this probe, revoked in cleanup
ZAHRA = 900000301
ALI = 900000302
BOT_ID = 8342690579
SYNTH_ROOMS = (ROOM,)

_seq = [900003500]


def _next_id():
    _seq[0] += 1
    return _seq[0]


def _user(uid, name="Probe", is_bot=False):
    return User(id=uid, is_bot=is_bot, first_name=name)


def _chat():
    return Chat(id=ROOM, type=ChatType.SUPERGROUP, title="probe")


class _Member:
    status = "administrator"
    can_restrict_members = True
    can_delete_messages = True
    can_promote_members = True
    can_manage_chat = True


sent: list[dict] = []


class _FakeBot:
    id = BOT_ID
    username = "mo3i_protect_bot"

    async def send_message(self, chat_id, text, **k):
        sent.append({"chat": chat_id, "text": text,
                     "reply_to": k.get("reply_to_message_id")})
        return None

    async def send_voice(self, chat_id, voice=None, **k):
        sent.append({"chat": chat_id, "text": "", "voice": True,
                     "reply_to": k.get("reply_to_message_id")})
        return None

    async def send_chat_action(self, *a, **k):
        return None

    async def get_chat_member(self, *a, **k):
        return _Member()


class _FakeCtx:
    bot = _FakeBot()


CTX = _FakeCtx()

MODE = "stub"
REAL_AWARENESS = chat.awareness
seen: list[dict] = []


async def _fake_awareness(transcript, context="", *, tools=None, on_tool=None):
    seen.append({"transcript": transcript, "context": context})
    if MODE == "real":
        reply = await REAL_AWARENESS(transcript, context, tools=tools, on_tool=on_tool)
        seen[-1]["raw"] = getattr(reply, "text", "") or ""
        seen[-1]["error"] = getattr(reply, "error", "") or ""
        return reply
    return chat.AwarenessReply(text=json.dumps(_SCRIPT), model="stub", turns=1)


_SCRIPT: dict = {}

_config = config


def _install():
    m.chat.awareness = _fake_awareness
    # One synthetic room sending rapidly: lift the per-user brake for this
    # process only. The probe is not testing the rate limiter.
    _config.GEMINI_CHAT_USER_RATE_LIMIT = 100
    _config.GEMINI_CHAT_USER_RATE_WINDOW = 1.0
    _config.GEMINI_CHAT_RATE_LIMIT = 100
    _config.GEMINI_CHAT_RATE_WINDOW = 1.0


def _clear():
    """Empty the synthetic room's window and state between scenarios."""
    with db._lock:
        for table in ("group_messages", "awareness_state"):
            db._conn.execute(f"DELETE FROM {table} WHERE chat_id=?", (ROOM,))
        db._conn.commit()
    awareness.reset_timers()
    m._nexus_addressed.clear()
    m._awareness_inflight.clear()
    m._awareness_last_pass.clear()
    sent.clear()
    seen.clear()


def _seed(rows):
    for mid, uid, name, text, extra in rows:
        db.group_capture(
            ROOM, uid, extra.get("role", "member"), name, text, keep=200,
            message_id=mid, directed=extra.get("directed", False),
            reply_user_id=extra.get("reply_user_id", 0),
            reply_name=extra.get("reply_name", ""),
        )


def _pending():
    real = [r for r in db.group_pending() if r["chat_id"] == ROOM]
    max_id = real[0]["max_id"] if real else 1
    now = int(time.time())
    return {"chat_id": ROOM, "oldest_at": now - 60, "newest_at": now - 60,
            "max_id": max_id, "pending": 1}


async def _pass(decision, mode="stub"):
    global MODE, _SCRIPT
    MODE = mode
    _SCRIPT = decision
    await m._awareness_pass(CTX, ROOM, _pending())


async def scenario(name, rows, *, decision, real=False):
    _clear()
    _seed(rows)
    # Pass 1: the server's reading, recorded. The model declines.
    await _pass({"relevant": True, "respond": False})
    reading = db.awareness_get(ROOM) or {}
    # Pass 2: a confident decision, to exercise the floor and the destination.
    await _pass(decision, mode="real" if real else "stub")
    spoke = bool(sent)
    raw = seen[-1].get("raw", "") if seen else ""
    error = seen[-1].get("error", "") if seen else ""
    return {
        "subject_kind": reading.get("subject_kind", ""),
        "subject_confidence": reading.get("subject_confidence", 0),
        "subject_message_id": reading.get("subject_message_id", 0),
        "spoke": spoke,
        "reply_to": sent[0]["reply_to"] if spoke else None,
        "context_has_reading": bool(seen and "read by the server" in seen[-1]["context"]),
        "answer": sent[0]["text"] if spoke else "",
        "model_error": error,
        "model_raw": raw[:400] if real else "",
    }


CONFIDENT = {"relevant": True, "respond": True, "message": "آره، خودمم 😄",
             "subject": "nexus", "participation": 85}
WEAK = {"relevant": True, "respond": True, "message": "بذار توضیح بدم",
        "subject": "general", "participation": 20}

Z, A = ZAHRA, ALI


async def main():
    db.init()
    groups.load()
    _install()
    nexus.reset_state()
    nexus.set_state(nexus.ONLINE)
    m._nexus_visibility[ROOM] = "administrator"
    m._bot_identity.update(
        id=BOT_ID, username="mo3i_protect_bot", name="Nexus",
        aliases=(), resolved=True,
    )

    owner = rbac.owner_id()
    out = {"probe_room": ROOM, "room_authorized_before": bool(m.authorized_group(ROOM))}
    groups.register(ROOM, actor_id=owner, title="probe")
    out["room_authorized_after"] = bool(m.authorized_group(ROOM))

    try:
        # 1. Talking about Nexus without tagging it.
        out["this_bot"] = await scenario(
            "this_bot",
            [(10, Z, "زهرا", "این ربات چقدر خوبه", {}),
             (11, A, "علی", "آره واقعاً", {})],
            decision=CONFIDENT,
        )
        # 2. "The bot" when the context clearly means Nexus.
        out["the_bot_why"] = await scenario(
            "the_bot_why",
            [(10, Z, "زهرا", "این ربات چرا اینقدر سریع جواب میده؟", {})],
            decision=CONFIDENT,
        )
        # 3. Discussing Nexus through a pronoun, after the subject is set.
        out["pronoun"] = await scenario(
            "pronoun",
            [(10, Z, "زهرا", "نکسوس اینو دیدی؟", {"directed": True}),
             (11, A, "علی", "آره، خیلی عجیبه", {}),
             (12, Z, "زهرا", "به نظرت خودش میفهمه داریم درباره‌ش حرف می‌زنیم؟", {})],
            decision=CONFIDENT,
        )
        # 4. A question about Nexus with no name anywhere.
        out["question_no_name"] = await scenario(
            "question_no_name",
            [(10, Z, "زهرا", "این هوش مصنوعی خیلی عجیبه", {}),
             (11, A, "علی", "چرا اینجوری جواب داد؟", {})],
            decision=CONFIDENT,
        )
        # 5. A natural conversation Nexus should enter at the right point.
        out["natural_entry"] = await scenario(
            "natural_entry",
            [(10, Z, "زهرا", "این ربات چقدر خوبه", {}),
             (11, A, "علی", "آره واقعاً", {})],
            decision=CONFIDENT,
        )
        # 6. Another bot / a general discussion about bots: stay out.
        out["general_bots"] = await scenario(
            "general_bots",
            [(10, Z, "زهرا", "ربات‌های تلگرام چطور کار می‌کنند؟", {})],
            decision=WEAK,
        )
        out["general_ai"] = await scenario(
            "general_ai",
            [(10, Z, "زهرا", "هوش مصنوعی‌ها چقدر ترسناکن", {})],
            decision=WEAK,
        )
        # 7. An unrelated conversation: no response at all.
        out["unrelated"] = await scenario(
            "unrelated",
            [(10, Z, "زهرا", "بچه‌ها فردا کلاس تعطیله؟", {}),
             (11, A, "علی", "نه بابا، شنبه امتحانه", {})],
            decision=WEAK,
        )
        # 8. One real-model pass, to show the reading reaches the model and the
        #    answer is a natural continuation rather than a report.
        if "--real" in sys.argv:
            out["real_this_bot"] = await scenario(
                "real_this_bot",
                [(10, Z, "زهرا", "این ربات چقدر خوبه", {}),
                 (11, A, "علی", "آره واقعاً", {})],
                decision={}, real=True,
            )
    finally:
        deleted, left = _cleanup()

    print("PROBE_JSON_START")
    print(json.dumps({
        "cleanup_deleted": deleted,
        "cleanup_rows_left": left,
        "scenarios": out,
    }, ensure_ascii=False, indent=1))
    print("PROBE_JSON_END")


def _cleanup():
    deleted = {}
    with db._lock:
        for table in ("authorized_groups", "group_messages", "chat_messages",
                      "people", "awareness_state", "conversation_state",
                      "user_memory", "admin_audit"):
            try:
                cur = db._conn.execute(
                    f"DELETE FROM {table} WHERE chat_id IN (?)", SYNTH_ROOMS)
                deleted[table] = cur.rowcount
            except Exception as exc:  # noqa: BLE001
                deleted[table] = f"error: {exc}"
        db._conn.commit()
    left = {}
    with db._lock:
        for table in ("authorized_groups", "group_messages", "awareness_state"):
            left[table] = db._conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE chat_id IN (?)",
                SYNTH_ROOMS).fetchone()[0]
    return deleted, left


asyncio.run(main())
