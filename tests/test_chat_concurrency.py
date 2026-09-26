"""The group under load: many people at once, nothing dropped, nothing mixed.

This is the behaviour the owner asked for, driven through the **real**
``main.on_group_chat`` rather than through the queue alone. A burst of messages
from several people in one room must all reach the model, each with its own
identity and its own context; no internal rate-limit sentence may be sent; and
one person's two messages must not overlap, because overlapping turns read the
same history and answer each other's context.

``chat.reply`` is replaced, so "how many calls, for whom, and in what order" is
asserted exactly. The stub sleeps, which is what creates the overlap the lock
must prevent — without the sleep every turn finishes before the next begins and
the test would pass whether or not the lock existed.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app import chat, chat_queue, config, db, groups, main, nexus, people, web_search

OWNER = 999
CHAT = -1001234567890
BOT_ID = 1


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(config, "CONFIG_ADMINS", [])
    monkeypatch.setattr(config, "GROUP_IDS", [CHAT])
    monkeypatch.setattr(config, "TMP_DIR", str(tmp_path))
    monkeypatch.setattr(config, "NEXUS_NAMES", ["nexus", "نکسوس"])
    monkeypatch.setattr(config, "NEXUS_PEOPLE_ENABLED", True)
    monkeypatch.setattr(config, "NEXUS_EXTRA_ACTION_WORDS", [])
    monkeypatch.setattr(config, "NEXUS_AWARENESS_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_AWARENESS_API_KEY", "test-awareness-key")
    monkeypatch.setattr(config, "NEXUS_AWARENESS_MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(config, "ADMIN_AI_ENABLED", True)
    monkeypatch.setattr(config, "ADMIN_TOOL_GUEST_TOOLS", False)
    monkeypatch.setattr(config, "BOT_ALIASES", [])
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "test-chat-key")
    monkeypatch.setattr("app.gemini_pool._pools", {})

    db.init()
    db.admin_reset()
    db.people_reset()
    db.nexus_state_reset()
    db.awareness_reset()
    db.authorized_groups_reset()
    groups.reset_state()
    nexus.reset_state()
    people.reset_state()
    chat.reset_state()
    chat_queue.reset_state()
    main._recently_deleted.clear()
    main._nexus_visibility.clear()
    main._nexus_visibility[CHAT] = "administrator"
    main._awareness_inflight.clear()
    main._awareness_last_pass.clear()
    main._awareness_urgent.clear()
    main._awareness_ready_at.clear()
    main._awareness_sweeping = False
    main._nexus_addressed.clear()
    main._bot_identity.update(
        id=BOT_ID, username="guardbot", name="Guard", aliases=(), resolved=True
    )
    yield
    chat_queue.reset_state()


class FakeBot:
    def __init__(self):
        self.id = BOT_ID
        self.username = "guardbot"
        self.messages: list[tuple] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))
        return SimpleNamespace(message_id=len(self.messages))

    async def send_chat_action(self, *args, **kwargs):
        pass

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(
            status="administrator",
            can_restrict_members=True,
            can_delete_messages=True,
            can_promote_members=True,
            can_manage_chat=True,
        )


def message(text, *, message_id=10, reply_to_message=None):
    return SimpleNamespace(
        message_id=message_id, photo=None, video=None, animation=None,
        video_note=None, sticker=None, voice=None, audio=None, document=None,
        text=text, caption=None, reply_to_message=reply_to_message,
    )


def update_for(msg, actor, *, username="tester", full_name="Tester"):
    return SimpleNamespace(
        effective_message=msg,
        effective_chat=SimpleNamespace(id=CHAT, type="supergroup", title="Group"),
        effective_user=SimpleNamespace(
            id=actor, full_name=full_name, username=username, is_bot=False
        ),
    )


def ctx_for(bot):
    return SimpleNamespace(bot=bot, args=[], application=None)


def install_model(monkeypatch, *, sleep=0.0, reply_for=None):
    """Replace ``chat.reply``. Records every turn and the peak overlap."""
    state = {"live": 0, "max": 0, "calls": []}

    async def _reply(chat_id, user_id, body, **kwargs):
        state["live"] += 1
        state["max"] = max(state["max"], state["live"])
        try:
            if sleep:
                await asyncio.sleep(sleep)
            if reply_for is not None:
                result = reply_for(user_id, body)
                if result is not None:
                    return result
            state["calls"].append({"chat_id": chat_id, "user_id": user_id, "text": body})
            return chat.ChatReply(answered=True, text=f"پاسخ {user_id}", turns=1)
        finally:
            state["live"] -= 1

    async def _no_search(question, *, history="", now=0.0):
        return web_search.Finding(ok=False, text="", sources=())

    monkeypatch.setattr(main.chat, "is_enabled", lambda: True)
    monkeypatch.setattr(main.chat, "reply", _reply)
    monkeypatch.setattr(main.web_search, "research", _no_search)
    return state


def drive(bot, updates):
    """Run several handler calls in one event loop, as Telegram would."""

    async def run():
        await asyncio.gather(
            *[main.on_group_chat(u, ctx_for(bot)) for u in updates]
        )

    asyncio.run(run())


# ── Ten people at once ─────────────────────────────────────────────────────
def test_ten_people_at_once_all_reach_the_model(monkeypatch):
    state = install_model(monkeypatch, sleep=0.01)
    bot = FakeBot()
    users = list(range(1001, 1011))

    drive(bot, [update_for(message(f"نکسوس سلام {u}"), u) for u in users])

    assert sorted(c["user_id"] for c in state["calls"]) == users
    assert len(bot.messages) == len(users)
    # No answer is the deployment's own scolding.
    assert all(text != chat._MESSAGES.get("rate_limit") for _, text in bot.messages)


def test_the_global_gate_bounds_a_burst(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_CHAT_MAX_CONCURRENCY", 3)
    state = install_model(monkeypatch, sleep=0.02)
    bot = FakeBot()

    drive(bot, [update_for(message(f"نکسوس سلام {u}"), u) for u in range(2001, 2011)])

    assert state["max"] <= 3
    assert len(state["calls"]) == 10


def test_one_persons_messages_never_overlap(monkeypatch):
    """Two messages sent together must not both read the old history."""
    state = install_model(monkeypatch, sleep=0.02)
    bot = FakeBot()
    user = 4242

    drive(
        bot,
        [
            update_for(message("نکسوس یکی", message_id=10), user),
            update_for(message("نکسوس دو", message_id=11), user),
            update_for(message("نکسوس سه", message_id=12), user),
        ],
    )

    assert state["max"] == 1
    assert len(state["calls"]) == 3
    assert [c["text"] for c in state["calls"]] == ["نکسوس یکی", "نکسوس دو", "نکسوس سه"]


def test_a_burst_does_not_mix_context_between_people(monkeypatch):
    """Each turn is told who is asking; the id is never another person's."""
    state = install_model(monkeypatch, sleep=0.01)
    bot = FakeBot()

    drive(bot, [update_for(message(f"نکسوس سلام {u}"), u) for u in range(3001, 3009)])

    ids = [c["user_id"] for c in state["calls"]]
    assert len(ids) == len(set(ids)) == 8
    assert all(c["chat_id"] == CHAT for c in state["calls"])


def test_a_throttled_turn_sends_no_message(monkeypatch):
    """The real path: a refusal from our own brake is silence, not a scolding."""
    state = install_model(
        monkeypatch,
        reply_for=lambda user_id, body: chat.ChatReply(
            answered=False, skipped="rate_limit"
        ),
    )
    bot = FakeBot()
    monkeypatch.setattr(config, "GEMINI_CHAT_QUEUE_MAX_WAIT", 0.0)

    drive(bot, [update_for(message("نکسوس سلام"), 5555)])

    assert state["calls"] == []
    assert bot.messages == []


def test_no_duplicate_answer_for_one_message(monkeypatch):
    """One message must produce exactly one send on the addressed path."""
    state = install_model(monkeypatch)
    bot = FakeBot()

    drive(bot, [update_for(message("نکسوس سلام"), 6666)])

    assert len(state["calls"]) == 1
    assert len(bot.messages) == 1
