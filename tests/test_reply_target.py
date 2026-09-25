"""What the user means, and which message Nexus answers.

Two readings of one message, and the tests keep them apart on purpose:

* the **semantic target** — the person or the message the current message is
  about. It comes from Telegram's own metadata (``reply_to_message``, the mention
  entities) and from the room's name memory, never from guessing.
* the **Telegram reply destination** — the message id the answer is sent as a
  reply to. It is the current message by default and moves to the resolved target
  only when the message actually asks for that («جواب اینو بده», «با این صحبت
  کن», «میلاد رو جواب بده»).

The integration tests drive the **real** ``on_group_chat`` handler and assert on
the ``reply_to_message_id`` the bot was actually handed, because the defect this
module closes was exactly that the resolved target never reached the send call.
``chat.reply`` is replaced, so "no model call happened" is exact.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app import chat, config, db, groups, main, nexus, people, reply_target, web_search

OWNER = 999
ADMIN = 556
MEMBER = 42
ZAHRA = 111
MILAD = 222
BOT_ID = 1
CHAT = -1001234567890
CURRENT = 500


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(config, "CONFIG_ADMINS", [f"{ADMIN}:admin"])
    monkeypatch.setattr(config, "GROUP_IDS", [CHAT])
    monkeypatch.setattr(config, "TMP_DIR", str(tmp_path))
    monkeypatch.setattr(config, "NEXUS_OBSERVE_ADMINS", True)
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
    main._recently_deleted.clear()
    main._nexus_visibility.clear()
    main._nexus_visibility[CHAT] = "administrator"
    main._awareness_inflight.clear()
    main._awareness_last_pass.clear()
    main._awareness_urgent.clear()
    main._awareness_ready_at.clear()
    main._awareness_sweeping = False
    main._bot_identity.update(
        id=BOT_ID, username="guardbot", name="Guard", aliases=(), resolved=True
    )
    yield
    db.admin_reset()
    db.people_reset()
    db.nexus_state_reset()
    db.awareness_reset()
    db.authorized_groups_reset()
    groups.reset_state()
    nexus.reset_state()
    people.reset_state()
    chat.reset_state()


# ── Fakes ─────────────────────────────────────────────────────────────────
class FakeBot:
    """Just enough of a bot, and a record of every send with its reply target."""

    def __init__(self):
        self.id = BOT_ID
        self.username = "guardbot"
        self.sent: list[dict] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(message_id=1000 + len(self.sent))

    async def send_voice(self, chat_id, voice=None, **kwargs):
        self.sent.append({"chat_id": chat_id, "voice": voice, **kwargs})
        return SimpleNamespace(message_id=1000 + len(self.sent))

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


def parent(message_id=480, user_id=ZAHRA, name="زهرا", text="این عکس خیلی قشنگه",
           caption=None, photo=None, reply_to_message=None):
    return SimpleNamespace(
        message_id=message_id,
        from_user=SimpleNamespace(id=user_id, full_name=name, username=""),
        text=text,
        caption=caption,
        reply_to_message=reply_to_message,
        photo=photo,
        video=None,
        animation=None,
        video_note=None,
        sticker=None,
        voice=None,
        audio=None,
        document=None,
    )


def message(text=None, *, message_id=CURRENT, reply_to_message=None,
            entities=None, caption=None, caption_entities=None):
    return SimpleNamespace(
        message_id=message_id,
        photo=None,
        video=None,
        animation=None,
        video_note=None,
        sticker=None,
        voice=None,
        audio=None,
        document=None,
        text=text,
        caption=caption,
        reply_to_message=reply_to_message,
        entities=entities or [],
        caption_entities=caption_entities or [],
    )


def update_for(msg, *, actor=MEMBER, chat_id=CHAT, full_name="Milad"):
    return SimpleNamespace(
        effective_message=msg,
        effective_chat=SimpleNamespace(id=chat_id, type="supergroup", title="Group"),
        effective_user=SimpleNamespace(
            id=actor, full_name=full_name, username="tester", is_bot=False
        ),
    )


def install_model(monkeypatch):
    """Replace ``chat.reply`` and the search. Returns the turns it was asked for."""
    calls: list[dict] = []

    async def _reply(chat_id, user_id, body, *, parts=None, kind="",
                     want_voice=False, tools=None, context="", on_tool=None):
        calls.append({"chat_id": chat_id, "user_id": user_id, "text": body,
                      "context": context})
        return chat.ChatReply(answered=True, text="باشه", turns=1)

    monkeypatch.setattr(main.chat, "is_enabled", lambda: True)
    monkeypatch.setattr(main.chat, "reply", _reply)
    monkeypatch.setattr(main.web_search, "enabled", lambda: False)
    return calls


def run_group(msg, bot, *, actor=MEMBER):
    ctx = SimpleNamespace(bot=bot, args=[], application=SimpleNamespace(bot=bot))
    asyncio.run(main.on_group_chat(update_for(msg, actor=actor), ctx))


def remember(user_id, first_name, *, last_name=""):
    people.remember(
        SimpleNamespace(id=user_id, first_name=first_name, last_name=last_name,
                        username="", is_bot=False),
        CHAT,
    )


def seed(text, *, user_id, message_id, name, reply_to=None, at=None, kind=""):
    db.group_capture(
        CHAT, user_id, "member", name, text,
        keep=200, message_id=message_id, kind=kind,
    )


# ══════════════════════════════════════════════════════════════════════════
# Part 1 — the reader, in isolation
# ══════════════════════════════════════════════════════════════════════════
def test_read_incoming_reads_the_replied_to_message():
    inc = reply_target.read_incoming(message("سلام", reply_to_message=parent()))
    assert inc.message_id == CURRENT
    assert inc.replied is not None
    assert inc.replied.message_id == 480
    assert inc.replied.user_id == ZAHRA
    assert inc.replied.name == "زهرا"
    assert inc.replied.text == "این عکس خیلی قشنگه"


def test_read_incoming_reads_a_caption_reply():
    replied = parent(text=None, caption="عکس از سفر")
    inc = reply_target.read_incoming(message("چیه؟", reply_to_message=replied))
    assert inc.replied.text == "عکس از سفر"


def test_read_incoming_reads_a_media_kind():
    photo = [SimpleNamespace(file_id="f", file_unique_id="u", file_size=10)]
    inc = reply_target.read_incoming(
        message("چیه؟", reply_to_message=parent(photo=photo))
    )
    assert inc.replied.kind == "photo"
    assert inc.replied.has_media is True


def test_read_incoming_reads_text_mention_entities():
    entity = SimpleNamespace(
        type="text_mention",
        user=SimpleNamespace(id=MILAD, full_name="میلاد", username=""),
    )
    inc = reply_target.read_incoming(message("سلام", entities=[entity]))
    assert len(inc.mentions) == 1
    assert inc.mentions[0].user_id == MILAD
    assert inc.mentions[0].name == "میلاد"


def test_read_incoming_reads_mentions_from_a_caption():
    entity = SimpleNamespace(type="mention", offset=0, length=6,
                             extract_from=lambda m: "@milad")
    inc = reply_target.read_incoming(
        message(None, caption="@milad سلام", caption_entities=[entity])
    )
    assert [m.username for m in inc.mentions] == ["milad"]


def test_read_incoming_never_raises_on_a_bare_object():
    inc = reply_target.read_incoming(SimpleNamespace())
    assert inc.message_id == 0
    assert inc.replied is None


def test_replied_identity_is_the_narrow_reading():
    assert reply_target.replied_identity(message("x")) == (0, "", 0)
    assert reply_target.replied_identity(message("x", reply_to_message=parent())) == (
        ZAHRA,
        "زهرا",
        480,
    )


def test_needs_window_only_for_a_reply_directive():
    assert reply_target.needs_window(reply_target.Incoming(), "این چیه؟") is False
    assert reply_target.needs_window(reply_target.Incoming(), "جواب اینو بده") is True


# ══════════════════════════════════════════════════════════════════════════
# Part 2 — resolution: the semantic target
# ══════════════════════════════════════════════════════════════════════════
def _resolve(text, *, replied=None, window=(), entities=None, current=CURRENT):
    incoming = reply_target.read_incoming(
        message(text, reply_to_message=replied, entities=entities)
    )
    return reply_target.resolve(
        text=text, incoming=incoming, chat_id=CHAT, window=window,
        current_message_id=current, bot_id=BOT_ID, bot_username="guardbot",
    )


def test_this_points_at_the_replied_to_message():
    target = _resolve("ببین این چیه", replied=parent())
    assert target.message_id == 480
    assert target.message_text == "این عکس خیلی قشنگه"
    assert target.person_id == 0


def test_this_person_points_at_the_replied_to_author():
    target = _resolve("این آدم کیه؟", replied=parent())
    assert target.person_id == ZAHRA
    assert target.person_name == "زهرا"
    assert target.message_id == 480


def test_a_bare_demonstrative_is_about_the_message_not_its_author():
    target = _resolve("این چیه؟", replied=parent())
    assert target.person_id == 0
    assert target.message_id == 480


def test_a_named_person_is_the_target_when_the_message_names_them():
    remember(MILAD, "میلاد")
    target = _resolve("با میلاد حرف بزن")
    assert target.person_id == MILAD
    assert target.person_name == "میلاد"


def test_a_text_mention_is_an_id_and_wins():
    entity = SimpleNamespace(
        type="text_mention",
        user=SimpleNamespace(id=MILAD, full_name="میلاد", username=""),
    )
    target = _resolve("به @میلاد جواب بده", entities=[entity])
    assert target.person_id == MILAD


def test_the_bots_own_mention_is_not_a_target():
    entity = SimpleNamespace(
        type="text_mention",
        user=SimpleNamespace(id=BOT_ID, full_name="Nexus", username=""),
    )
    target = _resolve("به @guardbot جواب بده", entities=[entity])
    assert target.person_id == 0


def test_two_names_are_not_resolved_by_picking_one():
    remember(MILAD, "میلاد")
    remember(ZAHRA, "زهرا")
    target = _resolve("با میلاد و زهرا حرف بزن")
    assert target.person_id == 0


def test_a_reply_chain_uses_the_immediate_parent():
    grandparent = parent(message_id=400, user_id=MILAD, name="میلاد", text="پیام قدیمی")
    replied = parent(message_id=480, text="پاسخ زهرا", reply_to_message=grandparent)
    target = _resolve("این چیه", replied=replied)
    assert target.message_id == 480
    assert target.message_text == "پاسخ زهرا"


# ══════════════════════════════════════════════════════════════════════════
# Part 3 — resolution: the Telegram reply destination
# ══════════════════════════════════════════════════════════════════════════
def test_a_plain_deictic_reply_moves_the_destination_to_the_parent():
    """«ببین این چیه» replying to a message is *about* that message.

    The earlier reading kept the answer under the asker's own message and only
    moved on an explicit directive. That was too literal: the reply edge plus
    «این» is already a reference, and the natural place for the answer is under
    the message it is about.
    """
    target = _resolve("ببین این چیه", replied=parent())
    assert target.reply_to == 480
    assert target.confidence == "reference"
    assert target.explicit is False
    assert target.destination(CURRENT) == 480


@pytest.mark.parametrize(
    "text",
    [
        "این چیه؟",
        "این چی میگه؟",
        "این رو ببین",
        "این دیگه چیه؟",
        "این طرف کیه؟",
        "اون چیه؟",
    ],
)
def test_a_lookup_reply_moves_the_destination_to_the_parent(text):
    target = _resolve(text, replied=parent())
    assert target.reply_to == 480
    assert target.confidence == "reference"
    assert target.destination(CURRENT) == 480


@pytest.mark.parametrize(
    "text",
    [
        "حرفش درسته؟",
        "پیامش چیه؟",
        "عکسش رو ببین",
        "ببین چی گفته",
        "آره دقیقاً",
        "آره واقعاً 😂",
    ],
)
def test_a_back_reference_moves_the_destination_to_the_parent(text):
    """A possessive, a third-person report or an agreement is about the parent."""
    target = _resolve(text, replied=parent())
    assert target.reply_to == 480
    assert target.confidence == "reference"
    assert target.destination(CURRENT) == 480


@pytest.mark.parametrize(
    "text",
    [
        "جواب اینو بده",
        "به این جواب بده",
        "با این صحبت کن",
        "با این طرف حرف بزن",
        "سر به سر این بذار",
        "به این پیام جواب بده",
        "جواب این پیام رو بده",
    ],
)
def test_an_explicit_directive_moves_the_destination_to_the_replied_to_message(text):
    target = _resolve(text, replied=parent())
    assert target.reply_to == 480
    assert target.explicit is True
    assert target.confidence == "explicit"
    assert target.destination(CURRENT) == 480


def test_a_stray_reply_verb_without_a_target_does_not_move_the_destination():
    target = _resolve("جواب ندادی", replied=parent())
    assert target.reply_to == 0
    assert target.destination(CURRENT) == CURRENT


def test_a_named_person_directive_uses_their_newest_message():
    remember(MILAD, "میلاد")
    window = [
        {"user_id": MILAD, "message_id": 470, "at": 900, "text": "سلام", "kind": ""},
        {"user_id": MILAD, "message_id": 490, "at": 990, "text": "خوبی؟", "kind": ""},
        {"user_id": ZAHRA, "message_id": 495, "at": 995, "text": "هی", "kind": ""},
    ]
    target = _resolve("میلاد رو جواب بده", window=window)
    assert target.person_id == MILAD
    assert target.reply_to == 490


def test_a_named_person_who_is_the_replied_to_author_uses_the_parent():
    remember(ZAHRA, "زهرا")
    window = [
        {"user_id": ZAHRA, "message_id": 450, "at": 900, "text": "قدیمی", "kind": ""},
    ]
    target = _resolve("زهرا رو جواب بده", replied=parent(), window=window)
    assert target.person_id == ZAHRA
    assert target.reply_to == 480


def test_an_unknown_name_does_not_move_the_destination():
    target = _resolve("رضا رو جواب بده")
    assert target.reply_to == 0
    assert target.destination(CURRENT) == CURRENT


def test_a_named_person_with_no_message_in_the_window_does_not_move_it():
    remember(MILAD, "میلاد")
    target = _resolve("میلاد رو جواب بده", window=[])
    assert target.person_id == MILAD
    assert target.reply_to == 0
    assert target.destination(CURRENT) == CURRENT


def test_a_message_id_written_in_the_text_is_not_a_destination():
    """The model is not the authority for ids, and neither is the message text."""
    target = _resolve("به پیام 480 جواب بده")
    assert target.reply_to == 0
    assert target.destination(CURRENT) == CURRENT


def test_the_destination_falls_back_to_the_current_message():
    target = _resolve("سلام")
    assert target.destination(CURRENT) == CURRENT
    assert target.destination(None) is None


# ══════════════════════════════════════════════════════════════════════════
# Part 4 — the block the model reads
# ══════════════════════════════════════════════════════════════════════════
def test_the_block_states_the_parent_its_author_and_its_words():
    out = reply_target.render(_resolve("این چیه", replied=parent()))
    assert "480" in out
    assert "زهرا" in out
    assert "این عکس خیلی قشنگه" in out
    assert "111" in out


def test_the_block_states_the_destination_when_it_moved():
    out = reply_target.render(_resolve("جواب اینو بده", replied=parent()))
    assert "480" in out
    assert "reply" in out.lower()


def test_the_block_is_empty_for_a_self_contained_message():
    assert reply_target.render(_resolve("سلام")) == ""


def test_the_block_is_bounded():
    long_text = "ک" * 5000
    out = reply_target.render(_resolve("این چیه", replied=parent(text=long_text)))
    assert len(out) <= reply_target.RENDER_CAP + 1


# ══════════════════════════════════════════════════════════════════════════
# Part 5 — the real handler: the destination reaches the send call
# ══════════════════════════════════════════════════════════════════════════
def test_a_plain_reply_is_sent_under_the_replied_to_message(monkeypatch):
    calls = install_model(monkeypatch)
    bot = FakeBot()
    run_group(message("@guardbot ببین این چیه", reply_to_message=parent()), bot)
    assert bot.sent, "the assistant answered"
    assert bot.sent[0]["reply_to_message_id"] == 480
    # ... and the model was handed the parent's words.
    assert "این عکس خیلی قشنگه" in calls[0]["context"]


@pytest.mark.parametrize(
    "text",
    ["این دیگه چیه؟", "این چی میگه؟", "آره دقیقاً", "حرفش درسته؟", "ببین چی گفته"],
)
def test_an_implicit_reference_reaches_the_send_call(text, monkeypatch):
    install_model(monkeypatch)
    bot = FakeBot()
    run_group(message(f"@guardbot {text}", reply_to_message=parent()), bot)
    assert bot.sent, "the assistant answered"
    assert bot.sent[0]["reply_to_message_id"] == 480


def test_a_reply_with_no_reference_stays_under_the_current_message(monkeypatch):
    install_model(monkeypatch)
    bot = FakeBot()
    run_group(
        message("@guardbot لطفا فردا ساعت ۵ یادم بنداز", reply_to_message=parent()),
        bot,
    )
    assert bot.sent, "the assistant answered"
    assert bot.sent[0]["reply_to_message_id"] == CURRENT


def test_a_reply_that_also_mentions_a_third_person_stays_put(monkeypatch):
    install_model(monkeypatch)
    bot = FakeBot()
    entity = SimpleNamespace(
        type="text_mention",
        user=SimpleNamespace(id=MILAD, full_name="میلاد", username=""),
    )
    run_group(
        message("@guardbot این چیه؟", reply_to_message=parent(), entities=[entity]),
        bot,
    )
    assert bot.sent[0]["reply_to_message_id"] == CURRENT


def test_an_explicit_reply_is_sent_under_the_replied_to_message(monkeypatch):
    install_model(monkeypatch)
    bot = FakeBot()
    run_group(message("@guardbot جواب اینو بده", reply_to_message=parent()), bot)
    assert bot.sent, "the assistant answered"
    assert bot.sent[0]["reply_to_message_id"] == 480


def test_this_person_reaches_the_model_as_the_replied_to_author(monkeypatch):
    calls = install_model(monkeypatch)
    bot = FakeBot()
    run_group(message("@guardbot این آدم کیه؟", reply_to_message=parent()), bot)
    assert bot.sent[0]["reply_to_message_id"] == 480
    assert "زهرا" in calls[0]["context"]
    assert "111" in calls[0]["context"]


def test_a_mention_without_a_reply_quotes_the_current_message(monkeypatch):
    calls = install_model(monkeypatch)
    bot = FakeBot()
    run_group(message("@guardbot سلام"), bot)
    assert bot.sent[0]["reply_to_message_id"] == CURRENT
    assert "replied-to message" not in calls[0]["context"]


def test_replying_to_nexus_own_message_is_answered_under_the_current_message(monkeypatch):
    install_model(monkeypatch)
    bot = FakeBot()
    own = parent(message_id=470, user_id=BOT_ID, name="Nexus", text="سلام، در خدمتم")
    run_group(message("این چیه", reply_to_message=own), bot)
    assert bot.sent, "a reply to the bot's own message is addressed to it"
    assert bot.sent[0]["reply_to_message_id"] == CURRENT


def test_a_caption_reply_is_read_and_the_kind_reaches_the_model(monkeypatch):
    calls = install_model(monkeypatch)
    bot = FakeBot()
    photo = [SimpleNamespace(file_id="f", file_unique_id="u", file_size=10)]
    replied = parent(text=None, caption="عکس از سفر", photo=photo)
    run_group(message("@guardbot این چیه", reply_to_message=replied), bot)
    assert "عکس از سفر" in calls[0]["context"]


def test_an_explicit_person_target_reaches_the_send_call(monkeypatch):
    remember(MILAD, "میلاد")
    seed("سلام بچه‌ها", user_id=MILAD, message_id=490, name="میلاد")
    install_model(monkeypatch)
    bot = FakeBot()
    run_group(message("@guardbot میلاد رو جواب بده"), bot)
    assert bot.sent[0]["reply_to_message_id"] == 490


def test_a_reply_chain_is_sent_under_the_immediate_parent(monkeypatch):
    install_model(monkeypatch)
    bot = FakeBot()
    grandparent = parent(message_id=400, user_id=MILAD, name="میلاد", text="پیام قدیمی")
    replied = parent(message_id=480, text="پاسخ زهرا", reply_to_message=grandparent)
    run_group(message("@guardbot جواب اینو بده", reply_to_message=replied), bot)
    assert bot.sent[0]["reply_to_message_id"] == 480


def test_an_unresolvable_directive_keeps_the_current_message(monkeypatch):
    install_model(monkeypatch)
    bot = FakeBot()
    run_group(message("@guardbot رضا رو جواب بده"), bot)
    assert bot.sent[0]["reply_to_message_id"] == CURRENT


def test_a_private_message_is_sent_unquoted(monkeypatch):
    install_model(monkeypatch)
    bot = FakeBot()
    msg = message("سلام", message_id=CURRENT)
    update = SimpleNamespace(
        effective_message=msg,
        effective_chat=SimpleNamespace(id=OWNER, type="private", title=""),
        effective_user=SimpleNamespace(
            id=OWNER, full_name="Owner", username="owner", is_bot=False
        ),
    )
    ctx = SimpleNamespace(bot=bot, args=[], application=SimpleNamespace(bot=bot))
    asyncio.run(main.on_private_text(update, ctx))
    assert bot.sent, "the owner is answered in private"
    assert bot.sent[0]["reply_to_message_id"] is None


# ══════════════════════════════════════════════════════════════════════════
# Part 6 — the block is a source the ceiling cannot drop
# ══════════════════════════════════════════════════════════════════════════
def test_the_relationship_block_is_never_dropped_by_the_ceiling():
    from app import context_plan

    reading = context_plan.read("این چیه؟", reply=True)
    plan = context_plan.compose(
        reading,
        target="TARGET-BLOCK",
        room="R" * 5000,
        awareness="A" * 5000,
        state="S" * 5000,
        memory="M" * 5000,
        ceiling=200,
    )
    assert "TARGET-BLOCK" in plan.text
