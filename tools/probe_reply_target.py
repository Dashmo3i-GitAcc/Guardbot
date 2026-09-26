"""Self-cleaning live probe of Telegram reply-target resolution, in-container.

Boundary under test (2026-09-25): the **semantic target** (who/what a message is
about) and the **Telegram reply destination** (which message id the answer is sent
as a reply to) are two readings of one message. The destination is graded:

* **explicit** — the message asks for the reply to go there («جواب اینو بده»,
  «با این صحبت کن», «میلاد رو جواب بده»); and
* **reference** — the message does not ask for anything but is *about* the
  replied-to message: a deictic («این چیه»), a possessive («حرفش»), a third-person
  report («ببین چی گفته») or a bare agreement («آره دقیقاً»).

A message with no reference of its own stays under itself, and a reference never
moves when it also points at a third person or when the parent is Nexus's own.

The scenario the owner reported is case ``reply_then_this``: somebody replies to
Zahra's message and writes «@Nexus ببین این چیه». Before the fix the model never
saw Zahra's words; now it is handed the parent's text and the answer is attached
to the message it is about.

The second reported scenario is the ``engage_*`` group (2026-09-26): somebody
replies to Zahra's message and writes «سر اینو گرم کن» or «با این چت کن» — and
never names Nexus. Before the fix that was not "addressed to the bot" by any
Telegram signal, so it never reached the conversation path at all and Nexus
answered the asker. It must now be answered, attached to Zahra's message, with
Zahra named as the person meant. The ``engage_false_positive`` and
``engage_stray_verb`` cases guard the other direction: «چای گرم کن» is tea and
«جواب ندادی» is a complaint, and neither may move anything.

There is no real Telegram round trip here — the harness has no second account to
reply from. The probe drives the **real** ``on_group_chat`` handler with real
``telegram.Message`` objects carrying real ``reply_to_message`` metadata, and the
send seam records the ``reply_to_message_id`` the bot was actually handed. Two
optional real-model turns (``--real``) prove the parent's words reach the model
and measure the length policy.

Synthetic group/user ids only. Everything it creates is deleted in a finally. The
real turns, when asked for, are the cost of a live probe and stay visible.

Run it against the **running** container (WORKDIR is ``/srv``, so the package is
not on ``sys.path`` for a script under ``/tmp`` — hence the explicit
``PYTHONPATH``)::

    docker cp tools/probe_reply_target.py guardbot:/tmp/
    docker exec -w /srv -e PYTHONPATH=/srv guardbot python /tmp/probe_reply_target.py

Add ``--real`` for the two real-model turns (``--real-long`` adds a third that
asks for a deliberately long answer, which is what exercises the split). Or run
it locally from the repo root.
"""
import asyncio
import datetime
import json
import sys

from telegram import Chat, Message, Update, User
from telegram.constants import ChatType

from app import chat, config, db, groups, nexus, people
from app import main as m
from app import rbac

ROOM = -1009000000021          # registered by this probe, revoked in cleanup
MEMBER = 900000201             # the person asking
ZAHRA = 900000202              # the person whose message is replied to
MILAD = 900000203              # a person named outright, with a stored message
SARA = 900000204               # a person named outright, with NO stored message
BOT_ID = 8342690579
SYNTH_ROOMS = (ROOM,)
SYNTH_USERS = (MEMBER, ZAHRA, MILAD, SARA)

PARENT_ID = 900001480          # Zahra's message
CURRENT_ID = 900001500         # the asker's message
MILAD_MSG_ID = 900001490       # Milad's newest stored message
PARENT_TEXT = "این عکس از سفر زعفران‌کاریمه"

# One synthetic speaker sending rapidly: lift the brake for this process only.
config.GEMINI_CHAT_USER_RATE_LIMIT = 100
config.GEMINI_CHAT_USER_RATE_WINDOW = 1.0
config.GEMINI_CHAT_RATE_LIMIT = 100
config.GEMINI_CHAT_RATE_WINDOW = 1.0

_seq = [900001600]


def _next_id():
    _seq[0] += 1
    return _seq[0]


def _user(uid, name="Probe", is_bot=False):
    return User(id=uid, is_bot=is_bot, first_name=name)


def _chat():
    return Chat(id=ROOM, type=ChatType.SUPERGROUP, title="probe")


def _parent(mid=PARENT_ID, uid=ZAHRA, name="زهرا", text=PARENT_TEXT):
    return Message(
        message_id=mid,
        date=datetime.datetime.now(datetime.timezone.utc),
        chat=_chat(),
        from_user=_user(uid, name),
        text=text,
    )


def _msg(text, *, reply_to=None, message_id=CURRENT_ID, uid=MEMBER):
    return Message(
        message_id=message_id,
        date=datetime.datetime.now(datetime.timezone.utc),
        chat=_chat(),
        from_user=_user(uid, "Asker"),
        text=text,
        reply_to_message=reply_to,
    )


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

calls: list[dict] = []      # chat.reply invocations (the model seam)
REAL_REPLY = chat.reply
MODE = "stub"


class _FakeResult:
    def __init__(self, text):
        self.text = text
        self.turns = 0
        self.timing = {}
        self.error = None
        self.skipped = ""
        self.answered = True
        self.truncated = False
        self.repeated = False
        self.voice = None

    def __bool__(self):
        return True


async def _fake_reply(chat_id, user_id, text, **kw):
    calls.append({"chat": chat_id, "user": user_id, "text": text,
                  "context": kw.get("context", "") or ""})
    if MODE == "real":
        return await REAL_REPLY(chat_id, user_id, text, **kw)
    return _FakeResult("[stubbed — no model call]")


async def _fake_capture(ctx, room, user, msg, text, principal, directed=False):
    return False


def _noop_schedule(*a, **k):
    return None


async def _false_cmd(*a, **k):
    return False


async def _noop(*a, **k):
    return None


def _fake_observe(room, user, msg, text):
    return True


def _install():
    chat.reply = _fake_reply
    m._awareness_capture = _fake_capture
    m._schedule_memory_observation = _noop_schedule
    m._schedule_state_observation = _noop_schedule
    m._awareness_schedule = _noop_schedule
    m._owner_voice_command = _false_cmd
    m._owner_state_command = _false_cmd
    m._awareness_promptly = _noop
    m._nexus_observe = _fake_observe
    # No live web lookup: the probe is about targets, not search, and a search
    # would spend a real request and could offer to search instead of answering.
    m.web_search.enabled = lambda: False


async def run(text, *, reply_to=None, mode="stub", uid=MEMBER):
    global MODE
    MODE = mode
    calls.clear()
    sent.clear()
    upd = Update(update_id=_next_id(), message=_msg(text, reply_to=reply_to, uid=uid))
    await m.on_group_chat(upd, CTX)
    answer_all = "".join(part.get("text") or "" for part in sent)
    return {
        "answer_sent": bool(sent),
        "reply_to": sent[0]["reply_to"] if sent else None,
        "messages_sent": len(sent),
        "context_has_parent_text": bool(calls and PARENT_TEXT in calls[0]["context"]),
        "context_has_zahra_id": bool(calls and str(ZAHRA) in calls[0]["context"]),
        "context_has_reply_to": bool(calls and "reply to message id" in calls[0]["context"]),
        # The person the message asked Nexus to address, when one resolved.
        "context_has_person": bool(calls and "The person meant is" in calls[0]["context"]),
        # Whether the message reached the conversation path at all. The routing
        # fix is what lets an engage reply that never names Nexus be answered.
        "answered_by_chat": bool(calls),
        # The server-controlled Telegram mention anchor, when a tag resolved.
        "mentions_milad": f"tg://user?id={MILAD}" in answer_all,
        "mentions_sara": f"tg://user?id={SARA}" in answer_all,
        "answer": sent[0]["text"] if sent else "",
        "answer_chars": len(answer_all),
        "context": calls[0]["context"] if calls else "",
    }


async def _cases(out):
    parent = _parent()

    # The reported scenario: reply to Zahra, mention Nexus, «این چیه».
    out["reply_then_this"] = await run("نکسوس ببین این چیه", reply_to=parent)
    # A lookup about a person: «این آدم».
    out["reply_then_this_person"] = await run("نکسوس این آدم کیه؟", reply_to=parent)
    # The implicit grade: the message refers back without asking for anything.
    out["reply_then_what_is_this"] = await run("نکسوس این دیگه چیه؟", reply_to=parent)
    out["reply_then_what_says_it"] = await run("نکسوس این چی میگه؟", reply_to=parent)
    out["reply_then_agreement"] = await run("نکسوس آره دقیقاً 😂", reply_to=parent)
    out["reply_then_possessive"] = await run("نکسوس حرفش درسته؟", reply_to=parent)
    out["reply_then_third_person"] = await run("نکسوس ببین چی گفته", reply_to=parent)
    # ... and the cases where nothing must move.
    out["reply_no_reference"] = await run(
        "نکسوس لطفا فردا ساعت ۵ یادم بنداز", reply_to=parent)
    # Explicit reply requests move the destination onto the parent.
    out["reply_then_answer_it"] = await run("نکسوس جواب اینو بده", reply_to=parent)
    out["reply_then_answer_this_msg"] = await run(
        "نکسوس به این پیام جواب بده", reply_to=parent)
    out["reply_then_talk_to_this"] = await run("نکسوس با این صحبت کن", reply_to=parent)
    out["reply_then_tease_this"] = await run("نکسوس سر به سر این بذار", reply_to=parent)
    # A named person with no reply: their newest stored message is the destination.
    out["named_person_no_reply"] = await run("نکسوس میلاد رو جواب بده")
    # A plain mention with no reply: nothing moves.
    out["plain_mention"] = await run("نکسوس سلام")
    # Reply to Nexus's own message: addressed, destination stays the asker's.
    own = _parent(mid=900001470, uid=BOT_ID, name="Nexus", text="در خدمتم")
    out["reply_to_bot"] = await run("این چیه", reply_to=own)
    # An unresolvable name must not invent a target.
    out["unknown_name"] = await run("نکسوس رضا رو جواب بده")

    # ── The tag directive (2026-09-26) ────────────────────────────────────
    # The owner's reported scenario, exactly: reply to Nexus's own message and
    # say «میلاد رو تگ کن». Before the fix «تگ» was not a directive word at all,
    # so the target resolved to nobody and the answer went out under the asker's
    # own message. It must now land under Milad's message and mention him.
    own_tag = _parent(mid=900001471, uid=BOT_ID, name="Nexus", text="چیزی لازم داری؟")
    out["tag_while_reply_to_bot"] = await run("میلاد رو تگ کن", reply_to=own_tag)
    # A tag with no reply: the named person's newest stored message.
    out["tag_plain"] = await run("نکسوس میلاد رو تگ کن")
    # A tag for somebody the room knows but who has no stored message: the answer
    # goes out unattached (never under the asker) and still mentions them.
    out["tag_with_no_message"] = await run("نکسوس سارا رو تگ کن")
    # A tag with a name nobody knows must not invent a target or a mention.
    out["tag_unknown_name"] = await run("نکسوس بهرام رو تگ کن")

    # ── The engage directive (2026-09-26) ─────────────────────────────────
    # The owner's other reported scenario, exactly: reply to Zahra's message and
    # say «سر اینو گرم کن» — *without* naming Nexus. Before the fix this was not
    # "addressed to the bot" by any Telegram signal, so it never reached the
    # conversation path and Nexus answered the asker. It must now be answered,
    # attached to Zahra's message, with Zahra named as the person meant.
    out["engage_no_name"] = await run("سر اینو گرم کن", reply_to=parent)
    out["engage_chat_with_this"] = await run("با این چت کن", reply_to=parent)
    out["engage_named_nexus"] = await run("نکسوس سر اینو گرم کن", reply_to=parent)
    # A harmless warm verb is not an engage directive and must not move.
    out["engage_false_positive"] = await run("چای گرم کن", reply_to=parent)
    # A stray reply verb with nothing to point at is a complaint, not a request.
    out["engage_stray_verb"] = await run("جواب ندادی", reply_to=parent)
    # An engage directive as a reply to Nexus's own message must not land under
    # the asker's message — that is the "it replies to me again" symptom.
    own_engage = _parent(mid=900001472, uid=BOT_ID, name="Nexus", text="چیزی لازم داری؟")
    out["engage_about_bot"] = await run("سر اینو گرم کن", reply_to=own_engage)

    # One real-model turn, to show the parent's words actually reach the model.
    if "--real" in sys.argv:
        out["real_reply_then_this"] = await run(
            "نکسوس ببین این چیه", reply_to=_parent(), mode="real")
        body = out["real_reply_then_this"]["answer"]
        out["real_reply_then_this"]["answer_mentions_parent_token"] = (
            "زعفران" in body or "سفر" in body or "عکس" in body
        )
        # The engage directive, end to end: a reply to Zahra that never names
        # Nexus must be answered *to Zahra* — the reported defect was that Nexus
        # replied to the asker instead.
        out["real_engage"] = await run("سر اینو گرم کن", reply_to=_parent(), mode="real")
        engage_body = out["real_engage"]["answer"]
        out["real_engage"]["answer_mentions_parent_token"] = (
            "زعفران" in engage_body
            or "سفر" in engage_body
            or "عکس" in engage_body
            or "زهرا" in engage_body
        )
        out["real_engage"]["answer_does_not_greet_the_asker"] = (
            "Asker" not in engage_body
        )
        # The length policy, end to end: an explicit ask for a full explanation
        # must produce a real answer and may arrive as several messages. Before
        # 2026-09-26 the persona's house length and a 1024-token ceiling cut this
        # to a few lines; `messages_sent > 1` is the split, not a truncation.
        out["real_full_answer"] = await run(
            "نکسوس لطفا کامل و با جزییات توضیح بده که اینترنت چطور کار می‌کنه",
            mode="real",
        )
    # The split itself needs an answer past one Telegram message, which only a
    # deliberately long ask produces. Its own flag so the ordinary `--real` run
    # stays two turns.
    if "--real-long" in sys.argv:
        out["real_long_answer"] = await run(
            "نکسوس لطفا خیلی کامل و مفصل، حدود دویست خط، همه‌چیز رو درباره "
            "اینترنت و شبکه توضیح بده — از صفر تا صد، با جزییات کامل",
            mode="real",
        )


def _cleanup():
    deleted = {}
    with db._lock:
        for table, col in (
            ("authorized_groups", "chat_id"),
            ("group_messages", "chat_id"),
            ("chat_messages", "chat_id"),
            ("people", "chat_id"),
            ("awareness_state", "chat_id"),
            ("conversation_state", "chat_id"),
            ("user_memory", "chat_id"),
            ("admin_audit", "chat_id"),
        ):
            try:
                ph = ",".join("?" * len(SYNTH_ROOMS))
                cur = db._conn.execute(
                    f"DELETE FROM {table} WHERE {col} IN ({ph})", SYNTH_ROOMS)
                deleted[table] = cur.rowcount
            except Exception as exc:  # noqa: BLE001
                deleted[table] = f"error: {exc}"
        db._conn.commit()
    left = {}
    with db._lock:
        for table, col in (
            ("authorized_groups", "chat_id"),
            ("group_messages", "chat_id"),
            ("chat_messages", "chat_id"),
            ("people", "chat_id"),
        ):
            ph = ",".join("?" * len(SYNTH_ROOMS))
            left[table] = db._conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {col} IN ({ph})",
                SYNTH_ROOMS).fetchone()[0]
    return deleted, left


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
    m._nexus_addressed.clear()

    owner = rbac.owner_id()
    real_rooms = list(config.GROUP_IDS)
    out = {"real_rooms_authorized": {str(r): bool(m.authorized_group(r)) for r in real_rooms},
           "probe_room_authorized_before": bool(m.authorized_group(ROOM))}

    groups.register(ROOM, actor_id=owner, title="probe")
    out["probe_room_authorized_after"] = bool(m.authorized_group(ROOM))

    # Seed the room window so the named-person case has a message to point at.
    people.remember(_user(MILAD, "میلاد"), ROOM)
    db.group_capture(ROOM, MILAD, "member", "میلاد", "سلام بچه‌ها", keep=200,
                     message_id=MILAD_MSG_ID)
    # Sara is known by name but has never had a message captured, so a tag for
    # her is the "resolved person with no held message" case.
    people.remember(_user(SARA, "سارا"), ROOM)

    try:
        await _cases(out)
    finally:
        deleted, left = _cleanup()

    print("PROBE_JSON_START")
    print(json.dumps({
        "probe_room": ROOM,
        "parent_message_id": PARENT_ID,
        "current_message_id": CURRENT_ID,
        "milad_message_id": MILAD_MSG_ID,
        "cleanup_deleted": deleted,
        "cleanup_rows_left": left,
        "cases": out,
    }, ensure_ascii=False, indent=1))
    print("PROBE_JSON_END")


asyncio.run(main())
