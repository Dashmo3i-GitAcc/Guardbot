"""A long answer arrives whole, as several messages instead of a summary.

The length policy has two halves and this file pins the second one. ``chat``
no longer cuts a reply to a house length (``test_chat.py`` pins that), and the
sender splits what is longer than one Telegram message at natural seams rather
than truncating it. The failure this prevents is the one the owner reported: he
asked for the news, the search ran, and the answer came back summarised.

The splitter is a pure function and is tested as one. ``_send_chat`` is then
driven directly with a recording bot, because that is the function that decides
how many messages a person actually receives and which of them carries the reply
target and the mention.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app import config, main


@pytest.fixture(autouse=True)
def layer(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_CHAT_REPLY_CHARS", 100)


class _Bot:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(message_id=1000 + len(self.sent))

    async def send_chat_action(self, *a, **k):
        pass


def _ctx(bot):
    return SimpleNamespace(bot=bot, args=[], application=SimpleNamespace(bot=bot))


def _send(text, *, reply_to=None, mention=None, bot=None):
    bot = bot or _Bot()
    ok = asyncio.run(main._send_chat(_ctx(bot), -100, text, reply_to, mention=mention))
    return ok, bot.sent


# ── The splitter, in isolation ────────────────────────────────────────────
def test_a_short_answer_is_a_single_untouched_message():
    assert main._split_for_telegram("سلام، خوبی؟", 100) == ["سلام، خوبی؟"]


def test_a_blank_line_is_the_preferred_seam():
    first = "الف" * 20  # 60 characters, comfortably under the 100 limit
    second = "ب" * 40  # 40 more, so the pair cannot fit one message
    chunks = main._split_for_telegram(f"{first}\n\n{second}", 100)
    assert chunks == [first, second]


def test_every_chunk_fits_the_limit():
    body = "یک جمله‌ی نسبتا بلند برای آزمودن برش. " * 20
    chunks = main._split_for_telegram(body, 100)
    assert len(chunks) > 1
    assert all(len(c) <= 100 for c in chunks)


def test_no_text_is_lost_across_the_seams():
    body = "یک جمله‌ی نسبتا بلند برای آزمودن برش. " * 20
    chunks = main._split_for_telegram(body, 100)
    # Whitespace at a seam is dropped, which is what a person would do too; the
    # words themselves must all survive.
    assert " ".join(chunks).split() == body.split()


def test_a_single_word_longer_than_the_limit_is_cut_not_dropped():
    body = "ا" * 250
    chunks = main._split_for_telegram(body, 100)
    assert "".join(chunks) == body
    assert all(len(c) <= 100 for c in chunks)


def test_an_empty_or_missing_answer_never_raises():
    assert main._split_for_telegram("", 100) == [""]
    assert main._split_for_telegram(None, 100) == [""]


# ── The send path ─────────────────────────────────────────────────────────
def test_a_long_answer_is_sent_as_several_messages_in_order():
    body = "یک جمله‌ی نسبتا بلند برای آزمودن برش. " * 20
    ok, sent = _send(body, reply_to=500)

    assert ok is True
    assert len(sent) > 1, "the answer was split, not cut"
    assert all(len(m["text"]) <= 100 for m in sent)
    joined = " ".join(m["text"] for m in sent)
    assert joined.split() == body.split(), "nothing was dropped"


def test_only_the_first_message_carries_the_reply_target():
    body = "یک جمله‌ی نسبتا بلند برای آزمودن برش. " * 20
    ok, sent = _send(body, reply_to=500)

    assert ok is True
    assert sent[0]["reply_to_message_id"] == 500
    assert all(m["reply_to_message_id"] is None for m in sent[1:])


def test_only_the_first_message_carries_the_mention():
    body = "یک جمله‌ی نسبتا بلند برای آزمودن برش. " * 20
    ok, sent = _send(body, mention=(222, "میلاد"))

    assert ok is True
    assert "tg://user?id=222" in sent[0]["text"]
    assert all("tg://user" not in m["text"] for m in sent[1:])


def test_a_short_answer_is_still_exactly_one_message():
    ok, sent = _send("باشه 🙂", reply_to=500)

    assert ok is True
    assert len(sent) == 1
    assert sent[0]["text"] == "باشه 🙂"
    assert sent[0]["reply_to_message_id"] == 500


def test_an_answer_is_escaped_before_it_is_split():
    """Model output is escaped per message, so no entity is cut in half."""
    body = "a<b> " * 40
    ok, sent = _send(body)

    assert ok is True
    assert all("<b>" not in m["text"] for m in sent)
    assert all("&lt;b&gt;" in m["text"] for m in sent)
