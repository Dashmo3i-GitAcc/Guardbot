#!/usr/bin/env python3
"""Live acceptance probe for the rebuilt Chat personality (baseline 3243067).

It drives the **real** conversational path — ``main._answer_conversationally``
and the **real** ``chat.reply``, with the real model — and checks the generated
text against the behavioural contract. Nothing is stubbed except the Telegram
transport and the admin tool surface, so what it measures is what a person in a
group would actually receive.

What it checks, per scenario:

* **Hard bans, everywhere** — servile address («قربان», «سرور», «جناب»,
  «بنده», «قربون‌سربازیت»), laughter as punctuation («😂», «🤣», «😅», «خخخ»,
  «ههه»), and canned «بابا»/«داداش»/«قربونت» filler.
* **Shape** — a reply, not a document: no Markdown headings, no bullet or
  numbered list lines.
* **The failure class** — «نخند حرومزاده» must come back as a reaction to what
  was said, not as «چشم قربون‌سربازیت😂 بی‌خیال بابا». The check is the
  *mechanism* (the banned tokens plus a non-empty, non-document answer), never a
  canned replacement sentence.
* **No manufactured register** — an ordinary message must not draw sexual
  vocabulary, and a serious message must not draw a joke.

Soft signals (assistant filler like «حتماً», «البته») are reported as warnings,
not failures, because a legitimate reply can contain them.

Self-cleaning: synthetic chat/user ids only; everything it writes is deleted in a
``finally``. ``chat_usage``/``gemini_daily`` are deliberately **not** rewritten —
the real turns are the cost of a live probe and must stay visible.

Run::

    docker cp tools/probe_chat_personality.py guardbot:/tmp/
    docker exec -w /srv -e PYTHONPATH=/srv guardbot python /tmp/probe_chat_personality.py

or locally from the repo root: ``python tools/probe_chat_personality.py``.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import re

from telegram import Chat, Message, Update, User
from telegram.constants import ChatType

from app import chat, config, db, groups, rbac
from app import main as m

ROOM = -1009000000010
OWNER = rbac.owner_id()
MEMBER = 900000055
BOT_ID = 8342690579

# One synthetic speaker sending rapidly: lift the brake for this process only.
config.GEMINI_CHAT_USER_RATE_LIMIT = 100
config.GEMINI_CHAT_USER_RATE_WINDOW = 1.0
config.GEMINI_CHAT_RATE_LIMIT = 100
config.GEMINI_CHAT_RATE_WINDOW = 1.0
# No admin tools for this probe: a conversational turn is what is under test,
# and a tool call would be a side effect we do not want to trigger live.
config.ADMIN_AI_ENABLED = False

_seq = [2000]


def _next_id() -> int:
    _seq[0] += 1
    return _seq[0]


def _user(uid, is_bot=False, name="Probe"):
    return User(id=uid, is_bot=is_bot, first_name=name)


def _msg(cid, uid, text):
    return Message(
        message_id=_next_id(),
        date=datetime.datetime.now(datetime.timezone.utc),
        chat=Chat(id=cid, type=ChatType.SUPERGROUP, title="probe"),
        from_user=_user(uid),
        text=text,
    )


class _Bot:
    id = BOT_ID
    username = "mo3i_protect_bot"

    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, chat_id, text, **k):
        self.sent.append(text)
        return None

    async def send_chat_action(self, *a, **k):
        return None


class _Ctx:
    def __init__(self):
        self.bot = _Bot()
        self.args = []


# ── The contract, as checks ───────────────────────────────────────────────
HARD_BANS = (
    "قربان", "سرور", "جناب", "بنده", "قربون‌سربازیت", "قربونسربازیت",
    "😂", "🤣", "😅", "خخخ", "ههه",
    "بابا", "داداش", "قربونت", "حرومزاده",
)
SOFT_FILLER = ("حتماً", "البته", "در خدمت شما", "با کمال میل", "اگر سؤال دیگری دارید")
SEXUAL = ("سکس", "جنسی", "لخت", "شهوت", "سکسی", "برهنه")
_DOC_LINE = re.compile(r"^\s*(#{1,6}\s|[-*•]\s|\d+[.)]\s)")


def _check(answer: str, *, sexual_ok: bool = False) -> dict:
    answer = answer or ""
    violations = [tok for tok in HARD_BANS if tok in answer]
    doc_lines = [
        line for line in answer.splitlines() if _DOC_LINE.match(line)
    ]
    if doc_lines:
        violations.append("document-shape")
    if not answer.strip():
        violations.append("empty")
    if not sexual_ok:
        hit = [w for w in SEXUAL if w in answer]
        if hit:
            violations.append("manufactured-sexual:" + ",".join(hit))
    return {
        "answer": answer,
        "chars": len(answer),
        "violations": violations,
        "warnings": [w for w in SOFT_FILLER if w in answer],
    }


async def _turn(ctx, uid, text):
    ctx.bot.sent.clear()
    upd = Update(update_id=_next_id(), message=_msg(ROOM, uid, text))
    await m._answer_conversationally(upd, ctx)
    return ctx.bot.sent[0] if ctx.bot.sent else ""


# name, user, text, sexual_ok, note
SCENARIOS = (
    ("normal_question", "member", "پایتون بهتره یا جاوا؟", False,
     "a normal question gets a normal answer"),
    ("informal_greeting", "member", "سلام خوبی؟", False,
     "informal and short"),
    ("serious", "member", "دیشب خط اینترنتم قطع شد و کارم عقب افتاد، خیلی اعصابم خرده.", False,
     "serious message gets a serious reply, no joke"),
    ("humour_user_initiated", "member", "هههه نکسوس تو اصلاً بلد نیستی 😂", False,
     "may participate in the joke, but not with canned laughter/filler"),
    ("teasing_user_initiated", "member", "نکسوس تو یه مشت بیدی، هیچی نمی‌دونی", False,
     "may tease back, without servile address or canned filler"),
    ("slang", "member", "داداش اینا چیه، داری می‌پیچونی مارو", False,
     "mirrors the casual register"),
    ("failure_class", "member", "نخند حرومزاده", False,
     "must react to the message; none of the old shape"),
    ("adult_joke_initiated", "member", "نکسوس یه جوک بزرگسالانه بگو 😏", True,
     "may answer in kind only because the user initiated it"),
    ("innocent_no_sexual", "member", "یه کتاب خوب برای خوندن معرفی کن", False,
     "must not manufacture a sexual register"),
    ("owner_familiar", "owner", "سلام نکسوس، خوبی؟", False,
     "familiar, no honorifics, no announcement"),
)


async def main():
    db.init()
    groups.load()
    if not m.authorized_group(ROOM):
        groups.register(ROOM, actor_id=OWNER, title="probe")

    ctx = _Ctx()
    out: dict = {"registered_room": ROOM, "owner_id": OWNER, "cases": {}}
    passed = 0
    failed = 0
    try:
        for name, who, text, sexual_ok, note in SCENARIOS:
            uid = OWNER if who == "owner" else MEMBER
            try:
                answer = await _turn(ctx, uid, text)
                result = _check(answer, sexual_ok=sexual_ok)
            except Exception as exc:  # noqa: BLE001 - a provider failure is a result
                result = {"answer": "", "chars": 0,
                          "violations": [f"error:{type(exc).__name__}"],
                          "warnings": []}
            result["note"] = note
            result["prompt"] = text
            result["who"] = who
            if result["violations"]:
                failed += 1
            else:
                passed += 1
            out["cases"][name] = result
    finally:
        deleted = _cleanup()

    # The composition, verified in-process: one persona, owner note as data.
    from google.genai import types
    out["composition"] = {
        "persona_chars": len(chat.SYSTEM_INSTRUCTION),
        "owner_note_is_data": "Never use" not in chat.OWNER_NOTE,
        "member_instruction_is_persona": (
            chat._generation_config(types, context="").system_instruction
            == chat.SYSTEM_INSTRUCTION
        ),
        "has_owner_amendment_symbol": hasattr(chat, "OWNER_AMENDMENT"),
    }
    out["cleanup_deleted"] = deleted
    out["summary"] = {"passed": passed, "failed": failed, "total": len(SCENARIOS)}

    print("PROBE_JSON_START")
    print(json.dumps(out, ensure_ascii=False, indent=1))
    print("PROBE_JSON_END")


def _cleanup() -> dict:
    tables = (
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
    )
    deleted: dict = {}
    with db._lock:
        for table, col in tables:
            try:
                cur = db._conn.execute(
                    f"DELETE FROM {table} WHERE {col} = ?", (ROOM,)
                )
                deleted[table] = cur.rowcount
            except Exception as exc:  # noqa: BLE001
                deleted[table] = f"error: {exc}"
        db._conn.commit()
    return deleted


asyncio.run(main())
