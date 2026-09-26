"""End to end: a real handler, a real archive.

This is the test the brief asks for — "prove real events reach the archive".
It drives ``main.on_group_chat`` with a message addressed to Nexus, through the
real gates and the real conversational path, and then reads the archive back to
assert the whole runtime story is there: what arrived, the routing decision, the
turn, the composed context, the model request and response, what Telegram
received, and the turn's end.

The second half is the other half of the contract: with observation on, the
handler must behave *exactly* as it does with observation off. The bot's own
output is compared across the two runs.
"""
import asyncio
import itertools
from types import SimpleNamespace

from app import chat, groups, main, nexus, observe
from app.observe import query, schema


CHAT = -1001234567890
UNREGISTERED = -1009999999999
BOT_ID = 999
MEMBER = 555

_UPDATES = itertools.count(1_000_000)


class FakeBot:
    def __init__(self):
        self.id = BOT_ID
        self.username = "guardbot"
        self.messages: list[str] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)
        return SimpleNamespace(message_id=len(self.messages))

    async def send_chat_action(self, *args, **kwargs):
        pass

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(
            status="member",
            can_restrict_members=False,
            can_delete_messages=False,
            can_promote_members=False,
            can_manage_chat=False,
        )


def _message(**fields):
    msg = SimpleNamespace(
        message_id=10,
        photo=None,
        video=None,
        animation=None,
        video_note=None,
        sticker=None,
        voice=None,
        audio=None,
        document=None,
        text=None,
        caption=None,
        reply_to_message=None,
    )
    for key, value in fields.items():
        setattr(msg, key, value)
    return msg


def _update(msg, *, actor=MEMBER, chat_id=CHAT):
    return SimpleNamespace(
        effective_message=msg,
        effective_chat=SimpleNamespace(id=chat_id, type="supergroup", title="Group"),
        effective_user=SimpleNamespace(
            id=actor, full_name="Tester", username="tester", is_bot=False
        ),
    )


def _install_model(monkeypatch, answer="باشه"):
    calls: list[dict] = []

    async def _reply(
        chat_id,
        user_id,
        body,
        *,
        parts=None,
        kind="",
        want_voice=False,
        tools=None,
        context="",
        on_tool=None,
    ):
        calls.append({"chat_id": chat_id, "user_id": user_id, "text": body})
        return chat.ChatReply(answered=True, text=answer, turns=1)

    async def _no_search(question, *, history="", now=0.0):
        from app import web_search

        return web_search.Finding(ok=False, text="", sources=())

    monkeypatch.setattr(main.chat, "is_enabled", lambda: True)
    monkeypatch.setattr(main.chat, "reply", _reply)
    monkeypatch.setattr(main.web_search, "research", _no_search)
    return calls


def _addressed_message():
    """A message that replies to the bot, which is how Nexus is addressed."""
    parent = SimpleNamespace(
        message_id=1, from_user=SimpleNamespace(id=BOT_ID, is_bot=True), text="قبلی"
    )
    return _message(text="سلام نکسوس", reply_to_message=parent)


async def _drive(monkeypatch, *, answer="باشه", msg=None, chat_id=CHAT):
    """Run one addressed group message through the real handler. Returns the bot."""
    groups.load()
    nexus.set_state(nexus.ONLINE)
    _install_model(monkeypatch, answer=answer)
    bot = FakeBot()
    ctx = SimpleNamespace(bot=bot, args=[], application=SimpleNamespace(bot=bot))
    update = _update(msg or _addressed_message(), chat_id=chat_id)
    # A fresh update id per drive, so the dedup guard never treats the second
    # run of a test as a replay of the first.
    update.update_id = next(_UPDATES)
    # The dispatcher runs the update guard (group -1) before the handler; mirror
    # it so `update.received` is recorded exactly as it is in production.
    await main.on_any_update(update, ctx)
    await main.on_group_chat(update, ctx)
    return bot


def test_a_real_turn_reaches_the_archive(archive, monkeypatch):
    async def scenario():
        await observe.start(worker=False)
        bot = await _drive(monkeypatch)
        await observe.flush()
        return bot

    bot = asyncio.run(scenario())
    assert bot.messages  # the handler really answered

    # The runtime story, in the archive.
    assert query.find(kind=schema.KIND_UPDATE, limit=10)
    assert query.find(kind=schema.KIND_ROUTING, event="directed", limit=10)
    assert query.find(kind=schema.KIND_CONTEXT, limit=10)
    assert query.find(kind=schema.KIND_AI, limit=10)
    assert query.find(kind=schema.KIND_AI_DONE, limit=10)
    assert query.find(kind=schema.KIND_DELIVERY, limit=10)

    # The turn is complete and reconstructable.
    found = query.turns()
    assert found["count"] == 1
    turn = found["turns"][0]
    assert turn["outcome"] == "sent"
    assert turn["chat_id"] == CHAT
    assert "سلام نکسوس" in (turn["inbound"] or "")
    assert "باشه" in (turn["outbound"] or "")

    traced = query.trace(turn["turn_id"])
    assert traced["found"] is True
    kinds = {event["kind"] for event in traced["events"]}
    assert {
        schema.KIND_CONTEXT,
        schema.KIND_AI,
        schema.KIND_AI_DONE,
        schema.KIND_DELIVERY,
    } <= kinds


def test_the_archive_holds_the_real_messages_not_metrics(archive, monkeypatch):
    async def scenario():
        await observe.start(worker=False)
        await _drive(monkeypatch, answer="چشم، انجام شد")
        await observe.flush()

    asyncio.run(scenario())
    # What the person said, and what Nexus answered, as text.
    assert query.search_events("سلام نکسوس")["count"] >= 1
    assert query.search_events("چشم، انجام شد")["count"] >= 1
    assert query.search_conversations("سلام نکسوس")["count"] >= 1


def test_the_context_the_model_saw_is_recorded(archive, monkeypatch):
    async def scenario():
        await observe.start(worker=False)
        await _drive(monkeypatch)
        await observe.flush()

    asyncio.run(scenario())
    composed = query.find(kind=schema.KIND_CONTEXT, limit=10)
    assert composed
    assert composed[0]["text"]  # the composed context itself, not a length


def test_observation_on_does_not_change_what_the_bot_says(archive, monkeypatch):
    """The whole point: telemetry is a sink, never an influence."""
    import app.config as config

    async def one():
        bot = await _drive(monkeypatch)
        await observe.flush()
        return bot.messages

    # Run 1: observation off.
    monkeypatch.setattr(config, "OBSERVE_ENABLED", False)

    async def off():
        await observe.stop()
        return await one()

    off_messages = asyncio.run(off())

    # Run 2: observation on, same input.
    monkeypatch.setattr(config, "OBSERVE_ENABLED", True)

    async def on():
        await observe.start(worker=False)
        return await one()

    on_messages = asyncio.run(on())
    assert on_messages == off_messages
    assert on_messages  # and it really did say something


def test_an_unregistered_room_is_recorded_as_a_boundary_refusal(archive, monkeypatch):
    """A room the bot does not serve is a recorded decision, not silence."""
    groups.load()  # seeds the allowlist from GROUP_IDS

    async def scenario():
        await observe.start(worker=False)
        _install_model(monkeypatch)
        bot = FakeBot()
        ctx = SimpleNamespace(bot=bot, args=[], application=SimpleNamespace(bot=bot))
        msg = _message(text="سلام", message_id=3)
        await main.on_group_chat(_update(msg, chat_id=UNREGISTERED), ctx)
        await observe.flush()
        return bot

    bot = asyncio.run(scenario())
    assert bot.messages == []  # nothing answered
    assert query.find(kind=schema.KIND_BOUNDARY, event="refused")


def test_a_duplicate_update_is_recorded(archive, monkeypatch):
    """The update guard's refusal is evidence, not an absence."""
    import app.config as config

    monkeypatch.setattr(config, "UPDATE_DEDUP_ENABLED", True)

    async def scenario():
        await observe.start(worker=False)
        update = _update(_message(text="hi", message_id=4))
        update.update_id = 424242
        ctx = SimpleNamespace(bot=FakeBot(), args=[])
        # First delivery claims the id; the second is a duplicate.
        await main.on_any_update(update, ctx)
        try:
            await main.on_any_update(update, ctx)
        except main.ApplicationHandlerStop:
            pass
        await observe.flush()

    asyncio.run(scenario())
    assert query.find(kind=schema.KIND_UPDATE, limit=10)
    assert query.find(kind=schema.KIND_DEDUP, event="dropped", limit=10)
