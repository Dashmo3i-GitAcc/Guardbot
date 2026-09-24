"""Self-cleaning live probe of the room boundary + tenant scoping, in-container.

Boundary under test (2026-09-24): the security boundary is the ROOM, not the
SPEAKER. A registered room is open to every member; an unregistered room is
refused before any identity write, awareness capture or model call.

Runs against the real config, the real DB-backed rbac, the real allowlist and
(for two turns) the real model. Synthetic group/user ids only. Everything it
creates is deleted in a finally. ``chat_usage``/``gemini_daily`` are deliberately
NOT rewritten — two real turns is the cost of a live probe and must stay visible.

Run it against the **running** container (the WORKDIR is ``/srv``, so the
package is not on ``sys.path`` for a script under ``/tmp`` — hence the explicit
``PYTHONPATH``)::

    docker cp tools/probe_room_boundary.py guardbot:/tmp/
    docker exec -w /srv -e PYTHONPATH=/srv guardbot python /tmp/probe_room_boundary.py

or locally from the repo root, ``python tools/probe_room_boundary.py``.
"""
import asyncio
import datetime
import json

from telegram import Chat, Message, Update, User
from telegram.constants import ChatType

from app import chat, config, db, groups
from app import main as m
from app import rbac

REG = -1009000000001      # registered by this probe, revoked in cleanup
UNREG = -1009000000002    # never registered
CMD = -1009000000003      # used for the typed command surface
OWNER = rbac.owner_id()
MEMBER = 900000042
STRANGER = 900000043
BOT_ID = 8342690579
SYNTH = (REG, UNREG, CMD)

# One synthetic speaker sending rapidly: lift the brake for this process only.
config.GEMINI_CHAT_USER_RATE_LIMIT = 100
config.GEMINI_CHAT_USER_RATE_WINDOW = 1.0
config.GEMINI_CHAT_RATE_LIMIT = 100
config.GEMINI_CHAT_RATE_WINDOW = 1.0

_seq = [1000]


def _next_id():
    _seq[0] += 1
    return _seq[0]


def _user(uid, is_bot=False, name="Probe"):
    return User(id=uid, is_bot=is_bot, first_name=name)


def _chat(cid, kind):
    return Chat(id=cid, type=kind, title="probe")


def _msg(cid, kind, uid, text, reply_to_bot=False):
    c = _chat(cid, kind)
    reply = None
    if reply_to_bot:
        reply = Message(
            message_id=_next_id(),
            date=datetime.datetime.now(datetime.timezone.utc),
            chat=c,
            from_user=_user(BOT_ID, is_bot=True, name="Nexus"),
            text="…",
        )
    return Message(
        message_id=_next_id(),
        date=datetime.datetime.now(datetime.timezone.utc),
        chat=c,
        from_user=_user(uid),
        text=text,
        reply_to_message=reply,
    )


class _Member:
    status = "administrator"


class _FakeBot:
    id = BOT_ID
    username = "mo3i_protect_bot"

    async def send_message(self, chat_id, text, **k):
        sent.append({"chat": chat_id, "text": text})
        return None

    async def send_chat_action(self, *a, **k):
        return None

    async def get_chat_member(self, *a, **k):
        return _Member()


class _FakeCtx:
    bot = _FakeBot()


CTX = _FakeCtx()

calls = []          # chat.reply invocations (the model seam)
sent = []           # bot.send_message invocations (the send seam)
awareness = []      # _awareness_capture invocations
remembered = []     # people.remember invocations

REAL_REPLY = chat.reply
MODE = "stub"


class _FakeResult:
    def __init__(self, text):
        self.text = text
        self.turns = 0
        self.timing = {}
        self.error = None
        self.skipped = False
        self.truncated = False
        self.repeated = False
        self.voice = None

    def __bool__(self):
        return True


async def _fake_reply(chat_id, user_id, text, **kw):
    calls.append({
        "chat": chat_id,
        "user": user_id,
        "text": text,
        "context": kw.get("context", "") or "",
    })
    if MODE == "real":
        return await REAL_REPLY(chat_id, user_id, text, **kw)
    return _FakeResult("[stubbed — no model call]")


async def _fake_capture(ctx, room, user, msg, text, principal, directed=False):
    awareness.append({"chat": room.id, "user": user.id})
    return False


def _fake_remember(user, chat_id):
    remembered.append({"chat": chat_id, "user": user.id})


def _noop_schedule(*a, **k):
    return None


async def _false_cmd(*a, **k):
    return False


async def _noop_promptly(*a, **k):
    return None


def _fake_observe(room, user, msg, text):
    return True


def _install():
    chat.reply = _fake_reply
    m._awareness_capture = _fake_capture
    m.people.remember = _fake_remember
    m._schedule_memory_observation = _noop_schedule
    m._schedule_state_observation = _noop_schedule
    m._awareness_schedule = _noop_schedule
    m._owner_voice_command = _false_cmd
    m._owner_state_command = _false_cmd
    m._awareness_promptly = _noop_promptly
    m._nexus_observe = _fake_observe


async def run(handler, cid, kind, uid, text, mode, reply_to_bot=True):
    global MODE
    MODE = mode
    calls.clear()
    sent.clear()
    awareness.clear()
    remembered.clear()
    upd = Update(update_id=_next_id(),
                 message=_msg(cid, kind, uid, text, reply_to_bot=reply_to_bot))
    await handler(upd, CTX)
    return {
        "model_calls": len(calls),
        "sent": [s["text"] for s in sent],
        "awareness_calls": len(awareness),
        "remember_calls": len(remembered),
        "caller": (calls[0]["user"] if calls else None),
        "owner_note": bool(
            calls and calls[0]["context"].startswith(chat.OWNER_NOTE)
        ),
    }


HONORIFICS = ["قربان", "سرور", "جناب", "بنده", "خدمتگزار", "اطاعت"]
FILLER = ["حتماً", "البته", "در خدمت شما", "اگر سؤال دیگری دارید", "خوشحال می‌شوم"]


def _scan(text):
    return {
        "honorifics": [h for h in HONORIFICS if h in (text or "")],
        "filler": [f for f in FILLER if f in (text or "")],
    }


async def _cases(out):
    # ── the room boundary ─────────────────────────────────────────────────
    out["unregistered_member"] = await run(
        m.on_group_chat, UNREG, ChatType.SUPERGROUP, MEMBER, "نکسوس سلام", "stub")
    out["unregistered_owner"] = await run(
        m.on_group_chat, UNREG, ChatType.SUPERGROUP, OWNER, "نکسوس سلام", "stub")
    out["registered_member_addressed"] = await run(
        m.on_group_chat, REG, ChatType.SUPERGROUP, MEMBER, "نکسوس سلام", "stub")
    out["registered_member_unaddressed"] = await run(
        m.on_group_chat, REG, ChatType.SUPERGROUP, MEMBER, "بچه‌ها هوا سرده",
        "stub", reply_to_bot=False)

    # ── owner-aware tone, real model ──────────────────────────────────────
    out["registered_member_real"] = await run(
        m.on_group_chat, REG, ChatType.SUPERGROUP, MEMBER, "سلام، خوبی؟", "real")
    out["registered_owner_real"] = await run(
        m.on_group_chat, REG, ChatType.SUPERGROUP, OWNER, "سلام، خوبی؟", "real")
    for tag in ("registered_member_real", "registered_owner_real"):
        body = out[tag]["sent"][0] if out[tag]["sent"] else ""
        out[tag]["reply"] = body
        out[tag]["scan"] = _scan(body)

    # ── private chat is unchanged ─────────────────────────────────────────
    out["private_stranger"] = await run(
        m.on_private_text, STRANGER, ChatType.PRIVATE, STRANGER, "سلام", "stub",
        reply_to_bot=False)
    out["private_owner"] = await run(
        m.on_private_text, OWNER, ChatType.PRIVATE, OWNER, "سلام", "stub",
        reply_to_bot=False)

    # ── revoking restores fail-closed ─────────────────────────────────────
    groups.revoke(REG, actor_id=OWNER)
    out["revoked_member"] = await run(
        m.on_group_chat, REG, ChatType.SUPERGROUP, MEMBER, "نکسوس سلام", "stub")
    groups.register(REG, actor_id=OWNER, title="probe")

    # ── the typed command surface (owner) ─────────────────────────────────
    out["cmd_groups_before"] = await _cmd("groups", CMD, OWNER)
    out["cmd_register"] = await _cmd("registergroup", CMD, OWNER)
    out["cmd_register_authorized"] = bool(groups.is_authorized(CMD))
    out["cmd_unregister"] = await _cmd("unregistergroup", CMD, OWNER)
    out["cmd_unregister_authorized"] = bool(groups.is_authorized(CMD))
    # a non-owner must not be able to register a room
    out["cmd_register_member"] = await _cmd("registergroup", UNREG, MEMBER)


async def _cmd(command, cid, uid):
    handler = {
        "groups": m.cmd_groups,
        "registergroup": m.cmd_register_group,
        "unregistergroup": m.cmd_unregister_group,
    }[command]
    calls.clear()
    sent.clear()
    upd = Update(update_id=_next_id(),
                 message=_msg(cid, ChatType.SUPERGROUP, uid, "/" + command,
                              reply_to_bot=False))
    ctx = _FakeCtx()
    ctx.args = []
    await handler(upd, ctx)
    return {"sent": [s["text"] for s in sent]}


def _cleanup():
    deleted = {}
    with db._lock:
        for table, col in (
            ("authorized_groups", "chat_id"),
            ("admin_requests", "chat_id"),
            ("admin_audit", "chat_id"),
            ("chat_messages", "chat_id"),
            ("people", "chat_id"),
            ("group_messages", "chat_id"),
            ("awareness_state", "chat_id"),
            ("conversation_state", "chat_id"),
            ("user_memory", "chat_id"),
            ("agent_tasks", "chat_id"),
        ):
            try:
                placeholders = ",".join("?" * len(SYNTH))
                cur = db._conn.execute(
                    f"DELETE FROM {table} WHERE {col} IN ({placeholders})", SYNTH)
                deleted[table] = cur.rowcount
            except Exception as exc:  # noqa: BLE001
                deleted[table] = f"error: {exc}"
        db._conn.commit()
    left = {}
    with db._lock:
        for table, col in (
            ("authorized_groups", "chat_id"),
            ("admin_requests", "chat_id"),
            ("admin_audit", "chat_id"),
            ("chat_messages", "chat_id"),
            ("people", "chat_id"),
            ("group_messages", "chat_id"),
        ):
            placeholders = ",".join("?" * len(SYNTH))
            left[table] = db._conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {col} IN ({placeholders})",
                SYNTH).fetchone()[0]
    return deleted, left


async def main():
    db.init()
    groups.load()
    _install()

    assert not rbac.resolve(MEMBER).is_admin, "MEMBER must be a guest"
    assert not rbac.resolve(STRANGER).is_admin, "STRANGER must be a guest"
    assert rbac.is_owner(OWNER), "OWNER must resolve as owner"

    # The production rooms must survive the deploy, and the synthetic one must
    # not exist until the probe registers it.
    real = list(config.GROUP_IDS)
    out = {"real_rooms_authorized": {str(r): bool(m.authorized_group(r)) for r in real},
           "unreg_authorized_before": bool(m.authorized_group(UNREG))}

    groups.register(REG, actor_id=OWNER, title="probe")
    out["reg_authorized_after_register"] = bool(m.authorized_group(REG))

    try:
        await _cases(out)
    finally:
        deleted, left = _cleanup()

    print("PROBE_JSON_START")
    print(json.dumps({
        "registered_room": REG,
        "unregistered_room": UNREG,
        "cleanup_deleted": deleted,
        "cleanup_rows_left": left,
        "cases": out,
    }, ensure_ascii=False, indent=1))
    print("PROBE_JSON_END")


asyncio.run(main())
