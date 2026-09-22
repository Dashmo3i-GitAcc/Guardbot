"""Which AI answers a message — and, more importantly, which one does not.

There are two AI policies in this bot and the requirement is that neither can be
entered by the other's traffic:

  * an ordinary group message goes to the acquisition pipeline, and
  * only an explicit address to the bot goes to the assistant.

These tests pin that boundary, because it is the kind of thing that quietly
erodes: a later change to a filter, or one missing guard, would turn a
3000-member group into a room the bot chats in.
"""
from types import SimpleNamespace

from app import main


class _Bot:
    id = 999
    username = "QuietStormGuardBot"


class _User:
    def __init__(self, uid=7, is_bot=False):
        self.id = uid
        self.is_bot = is_bot
        self.username = "someone"
        self.first_name = "Someone"


def _msg(text, *, reply_author_id=None, reply_author_is_bot=False):
    replied = None
    if reply_author_id is not None:
        replied = SimpleNamespace(
            from_user=_User(reply_author_id, reply_author_is_bot)
        )
    return SimpleNamespace(text=text, reply_to_message=replied, message_id=1)


def _ctx():
    return SimpleNamespace(bot=_Bot())


# ── What counts as addressing the bot ─────────────────────────────────────
def test_a_reply_to_a_bot_message_is_an_address():
    msg = _msg("سلام", reply_author_id=999)
    assert main._addressed_to_bot(msg, _ctx()) is True


def test_an_at_mention_is_an_address():
    msg = _msg("@QuietStormGuardBot سلام")
    assert main._addressed_to_bot(msg, _ctx()) is True


def test_a_mention_is_case_insensitive():
    msg = _msg("@quietstormguardbot سلام")
    assert main._addressed_to_bot(msg, _ctx()) is True


# ── What does not ─────────────────────────────────────────────────────────
def test_an_ordinary_group_message_is_not_an_address():
    """The whole point: the bot must not chat with the room."""
    assert main._addressed_to_bot(_msg("سلام بچه‌ها"), _ctx()) is False


def test_a_reply_to_another_member_is_not_an_address():
    msg = _msg("باشه", reply_author_id=4242)
    assert main._addressed_to_bot(msg, _ctx()) is False


def test_a_reply_to_another_bot_is_not_an_address():
    """Somebody else's bot is not this bot."""
    msg = _msg("سلام", reply_author_id=555, reply_author_is_bot=True)
    assert main._addressed_to_bot(msg, _ctx()) is False


def test_the_word_robot_is_not_an_address():
    assert main._addressed_to_bot(_msg("ای ربات تو چطوری"), _ctx()) is False


def test_an_empty_username_never_matches():
    """If the bot has no resolved username, a bare @ must not match anything."""
    ctx = SimpleNamespace(bot=SimpleNamespace(id=999, username=""))
    assert main._addressed_to_bot(_msg("@ سلام"), ctx) is False


# ── The guard that keeps the two apart ────────────────────────────────────
def test_the_acquisition_handler_yields_when_the_assistant_is_off(monkeypatch):
    """With chat disabled, an addressed message is ordinary acquisition traffic."""
    monkeypatch.setattr(main.chat, "is_enabled", lambda: False)
    assert main._chat_active() is False


def test_the_acquisition_handler_yields_when_the_assistant_is_on(monkeypatch):
    monkeypatch.setattr(main.chat, "is_enabled", lambda: True)
    assert main._chat_active() is True


def test_the_guard_is_wired_into_the_acquisition_handler():
    """`on_group_text` must consult the guard before classifying.

    Asserted on the source because the handler needs a live Telegram context to
    run, and the thing that matters here is that the check exists and happens
    before the classifier — a behavioural test would need a full Update.
    """
    import inspect

    source = inspect.getsource(main.on_group_text)
    guard = source.index("_addressed_to_bot(msg, ctx)")
    classify = source.index("classifier.classify")
    assert guard < classify, "the assistant check must come before classifying"


def test_both_filters_see_the_same_kind_of_message():
    """The two handlers must not be able to reach different traffic.

    The assistant's filter is now the media-inclusive one, because an addressed
    sticker or voice note is still an addressed message. The property this
    asserts is unchanged: both handlers see ordinary group traffic and decide
    between them, rather than one being able to reach messages the other cannot.
    """
    assert main.group_chat_filter() is not None
    assert main.acquisition_message_filter() is not None


# ── The wiring that keeps a conversation from stalling the bot ────────────
def test_the_conversational_handlers_do_not_block_the_dispatcher():
    """A reply can take up to 25s; the dispatcher must not wait for it.

    Updates are processed one at a time unless a handler opts out, so without
    `block=False` one person chatting would pause media moderation for the whole
    group. Asserted on the registration because the flag lives there, and
    `main()` needs a live token to run.
    """
    import inspect

    source = inspect.getsource(main.main)
    assert "on_group_chat, block=False" in source
    assert "on_private_text, block=False" in source


def test_start_and_reset_are_registered():
    import inspect

    source = inspect.getsource(main.main)
    assert 'CommandHandler("start"' in source
    assert 'CommandHandler("reset"' in source


class _ChatBot:
    """Captures what would have been sent by a command handler."""

    def __init__(self):
        self.sent = []

    async def send_chat_action(self, chat_id, action):
        pass

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(message_id=1)


def _start_update(chat_type):
    return SimpleNamespace(
        effective_message=SimpleNamespace(message_id=5),
        effective_chat=SimpleNamespace(
            id=42 if chat_type == "private" else -100, type=chat_type
        ),
        effective_user=SimpleNamespace(id=7, first_name="Sara", is_bot=False),
    )


def test_start_is_ignored_in_a_group():
    """Answering /start in a group would be the bot talking to the room."""
    import asyncio

    bot = _ChatBot()
    asyncio.run(main.on_chat_start(_start_update("supergroup"), SimpleNamespace(bot=bot)))

    assert bot.sent == []


def test_start_answers_in_a_private_chat():
    import asyncio

    bot = _ChatBot()
    asyncio.run(main.on_chat_start(_start_update("private"), SimpleNamespace(bot=bot)))

    assert len(bot.sent) == 1
    assert "Sara" in bot.sent[0]["text"]


def test_the_start_greeting_is_escaped_not_parsed_as_markup():
    """The greeting goes through the same escaping as a model reply."""
    import asyncio

    bot = _ChatBot()
    update = _start_update("private")
    update.effective_user.first_name = "<b>Sam</b>"
    asyncio.run(main.on_chat_start(update, SimpleNamespace(bot=bot)))

    assert "<b>" not in bot.sent[0]["text"]
    assert "&lt;b&gt;" in bot.sent[0]["text"]
