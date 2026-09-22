"""Join verification: a completed challenge can never be undone by a timer.

The bug this suite exists to pin, reported from production: a member joined, was
shown "من ربات نیستم", pressed it, was told «✅ تأیید شد» — and was kicked about
two minutes later anyway.

The cause was an ordering mistake, not a timeout that was too short. Three
things were wrong together:

* ``db.get_captcha`` ignored the deadline, so a press that arrived *after* the
  120s were up was still honoured: the member was unmuted and told they had
  verified.
* The reaper decided from a single snapshot (``db.expired_captchas``) and never
  re-read state per row, so it kicked that member anyway.
* The press handler deleted the challenge row *after* awaiting
  ``restrict_chat_member``, so for the length of that round-trip the row still
  said "pending" and a reaper tick could act on it.

Verification and expiry are now a compare-and-swap on one row — the deletion is
the claim, and only the winner acts — and the click refuses an expired row
instead of pretending to succeed.

Everything here runs the real handlers against a fake Telegram, and the real
``app/db.py`` against a fresh in-memory database.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest
from telegram.error import TelegramError

from app import config, db, main

CHAT = -1001234567890
OTHER_CHAT = -1007777777777
JOINER = 4242
OTHER_JOINER = 4343


# ── Harness ───────────────────────────────────────────────────────────────
class FakeBot:
    """The Telegram calls the captcha flow can make, all recorded.

    Muting and unmuting are the same Telegram call, so they are told apart by
    the permissions object: ``main.MUTED`` cannot send messages, ``main.FULL``
    can. Without that the join's mute and a verification's unmute would look
    identical in the log and half these assertions would be vacuous.
    """

    def __init__(self, *, unmute_fails=False, is_admin=False):
        self.sent: list[tuple[int, str]] = []
        self.muted: list[tuple[int, int]] = []
        self.unmuted: list[tuple[int, int]] = []
        self.banned: list[tuple[int, int]] = []
        self.unbanned: list[tuple[int, int]] = []
        self.deleted: list[tuple[int, int]] = []
        self.unmute_fails = unmute_fails
        self.admin = is_admin
        self._next_id = 500

    async def send_message(self, chat_id, text, reply_markup=None):
        self._next_id += 1
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=self._next_id)

    async def restrict_chat_member(self, chat_id, user_id, permissions=None, **kw):
        can_post = getattr(permissions, "can_send_messages", None)
        if can_post:
            if self.unmute_fails:
                raise TelegramError("not enough rights to restrict")
            self.unmuted.append((chat_id, user_id))
        else:
            self.muted.append((chat_id, user_id))

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="administrator" if self.admin else "member")

    async def ban_chat_member(self, chat_id, user_id, **kw):
        self.banned.append((chat_id, user_id))

    async def unban_chat_member(self, chat_id, user_id, **kw):
        self.unbanned.append((chat_id, user_id))

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class FakeCallback:
    def __init__(self, chat_id, message_id=900):
        self.answers: list[tuple[str, bool]] = []
        self.message = SimpleNamespace(
            chat=SimpleNamespace(id=chat_id), message_id=message_id
        )
        self.deleted_message = False

    async def answer(self, text="", show_alert=False):
        self.answers.append((text, show_alert))

    @property
    def last_answer(self):
        return self.answers[-1][0] if self.answers else ""


def ctx_for(bot):
    return SimpleNamespace(bot=bot, args=[], application=SimpleNamespace(bot=bot))


def join_update(user_id=JOINER, chat_id=CHAT, old="left", new="member", is_bot=False):
    user = SimpleNamespace(id=user_id, full_name="Newcomer", is_bot=is_bot)
    return SimpleNamespace(
        chat_member=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            old_chat_member=SimpleNamespace(status=old),
            new_chat_member=SimpleNamespace(status=new, user=user),
        )
    )


def click_update(user_id=JOINER, chat_id=CHAT, message_id=900, button_owner=None):
    """``button_owner`` is whose button was pressed; ``user_id`` is who pressed."""
    q = FakeCallback(chat_id, message_id)
    q.data = f"cap:{JOINER if button_owner is None else button_owner}"
    q.from_user = SimpleNamespace(id=user_id, full_name="Newcomer")

    async def _delete():
        q.deleted_message = True

    q.message.delete = _delete
    return SimpleNamespace(callback_query=q), q


@pytest.fixture(autouse=True)
def captcha_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "GROUP_IDS", [CHAT, OTHER_CHAT])
    monkeypatch.setattr(config, "CAPTCHA_ENABLED", True)
    monkeypatch.setattr(config, "CAPTCHA_TIMEOUT_SEC", 120)
    monkeypatch.setattr(config, "CAPTCHA_RETRY_GRACE_SEC", 15)
    monkeypatch.setattr(config, "CAPTCHA_TEXT", "سلام {name} — {timeout} ثانیه")
    monkeypatch.setattr(config, "CAPTCHA_BUTTON", "من ربات نیستم")
    monkeypatch.setattr(config, "WHITELIST_USER_IDS", set())
    monkeypatch.setattr(config, "TMP_DIR", str(tmp_path))
    db.init()
    main._admin_cache.clear()
    yield
    main._admin_cache.clear()


def join(bot, **kw):
    asyncio.run(main.on_member_update(join_update(**kw), ctx_for(bot)))


def click(bot, **kw):
    update, q = click_update(**kw)
    asyncio.run(main.on_captcha_click(update, ctx_for(bot)))
    return q


def reap(bot):
    asyncio.run(main.captcha_reaper(ctx_for(bot)))


def expire(chat_id=CHAT, user_id=JOINER):
    """Push a pending challenge past its deadline, the way time would."""
    row = db.get_captcha(chat_id, user_id)
    assert row is not None, "nothing to expire"
    db.add_captcha(chat_id, user_id, row[0], int(time.time()) - 1)


# ══ 1. The reported bug ═══════════════════════════════════════════════════
def test_a_verified_member_is_never_kicked_by_the_timer():
    """join → press → verify → the timer fires → no ban. The whole report."""
    bot = FakeBot()
    join(bot)
    assert db.get_captcha(CHAT, JOINER) is not None

    q = click(bot)
    assert q.last_answer == "✅ تأیید شد"
    assert bot.unmuted == [(CHAT, JOINER)]
    assert db.get_captcha(CHAT, JOINER) is None, "the challenge outlived the click"

    # The timer runs afterwards — and has nothing to act on.
    reap(bot)
    assert bot.banned == []
    assert bot.unbanned == []


def test_a_late_press_is_never_told_it_verified():
    """The exact shape of the report: a press around the 120s mark.

    Before the fix this press was honoured — the member was unmuted and told
    «✅ تأیید شد» — and the reaper then kicked them anyway, because the challenge
    row ignored its own deadline. Now the press is refused honestly.
    """
    bot = FakeBot()
    join(bot)
    expire()

    q = click(bot)

    assert q.last_answer == "مهلت تمام شده."
    assert bot.unmuted == [], "a member past their deadline was let in"
    assert "تأیید" not in q.last_answer


def test_a_late_press_does_not_save_the_member_from_enforcement():
    """The fix must not turn "too late" into "let them in"."""
    bot = FakeBot()
    join(bot)
    expire()
    click(bot)

    reap(bot)

    assert bot.banned == [(CHAT, JOINER)]
    assert db.get_captcha(CHAT, JOINER) is None


def test_the_reaper_does_not_act_on_a_snapshot_that_went_stale(monkeypatch):
    """The reaper's query result is a work queue, not a verdict.

    This is the interleaving that produced the report, expressed directly: the
    reaper queries its list, a member verifies before the reaper reaches their
    row, and the reaper must then do nothing at all.
    """
    bot = FakeBot()
    join(bot)
    stale = db.expired_captchas(int(time.time()) + 10_000)
    assert stale, "the harness did not produce a snapshot"

    # The member verifies in the gap between the query and the ban.
    db.remove_captcha(CHAT, JOINER)

    monkeypatch.setattr(db, "expired_captchas", lambda now: stale)
    reap(bot)

    assert bot.banned == [], "a member who verified was kicked by a stale timer"


def test_the_reaper_still_kicks_when_nobody_verified():
    """The fix must not disable enforcement."""
    bot = FakeBot()
    join(bot)
    expire()

    reap(bot)

    assert bot.banned == [(CHAT, JOINER)]
    assert bot.unbanned == [(CHAT, JOINER)], "ban+unban is how a kick is done"
    assert db.get_captcha(CHAT, JOINER) is None


def test_an_unexpired_challenge_is_left_alone():
    bot = FakeBot()
    join(bot)
    reap(bot)
    assert bot.banned == []
    assert db.get_captcha(CHAT, JOINER) is not None, "the member is still on the clock"


# ══ 2. The press ═════════════════════════════════════════════════════════
def test_a_second_press_is_a_no_op():
    """Idempotent: the row is the claim, so the second press finds nothing."""
    bot = FakeBot()
    join(bot)
    first = click(bot)
    second = click(bot)

    assert first.last_answer == "✅ تأیید شد"
    assert second.last_answer == "مهلت تمام شده."
    assert len(bot.unmuted) == 1, "the second press unmuted again"


def test_a_press_from_somebody_else_is_refused():
    """The button is bound to a user id, not to whoever presses it first."""
    bot = FakeBot()
    join(bot)
    q = click(bot, user_id=OTHER_JOINER, button_owner=JOINER)

    assert q.last_answer == "این دکمه برای شما نیست."
    assert bot.unmuted == []
    assert db.get_captcha(CHAT, JOINER) is not None, "the real member's row was eaten"


def test_a_press_in_another_chat_cannot_verify_the_challenged_one():
    """The callback carries a user id, so the chat has to come from the message."""
    bot = FakeBot()
    join(bot, chat_id=CHAT)
    q = click(bot, chat_id=OTHER_CHAT)

    assert q.last_answer == "مهلت تمام شده."
    assert db.get_captcha(CHAT, JOINER) is not None
    assert bot.unmuted == []


def test_a_failed_unmute_puts_the_challenge_back():
    """A transient Telegram error must not strand the member muted with no row."""
    bot = FakeBot(unmute_fails=True)
    join(bot)
    q = click(bot)

    assert q.last_answer == "خطا، دوباره امتحان کن."
    row = db.get_captcha(CHAT, JOINER)
    assert row is not None, "the row was not restored"
    # And the restored row is in the future, so a retry is actually possible.
    assert row[1] > int(time.time())


def test_a_retry_after_a_failed_unmute_succeeds():
    bot = FakeBot(unmute_fails=True)
    join(bot)
    click(bot)

    bot.unmute_fails = False
    q = click(bot)
    assert q.last_answer == "✅ تأیید شد"
    assert db.get_captcha(CHAT, JOINER) is None


def test_a_failed_unmute_then_a_retry_that_is_also_late_is_enforced():
    """The grace period is a grace period, not an exemption."""
    bot = FakeBot(unmute_fails=True)
    join(bot)
    click(bot)
    expire()

    click(bot)
    reap(bot)
    assert bot.banned == [(CHAT, JOINER)]


# ══ 3. Joining ═══════════════════════════════════════════════════════════
def test_a_duplicate_join_event_does_not_stack_a_second_challenge():
    bot = FakeBot()
    join(bot)
    first = db.get_captcha(CHAT, JOINER)
    join(bot)

    assert len(bot.sent) == 1, "a second challenge was sent"
    assert db.get_captcha(CHAT, JOINER) == first, "the deadline was pushed back"


def test_a_bot_is_never_challenged():
    bot = FakeBot()
    join(bot, is_bot=True)
    assert bot.sent == []
    assert db.get_captcha(CHAT, JOINER) is None


def test_an_admin_joining_is_never_challenged():
    bot = FakeBot(is_admin=True)
    join(bot)
    assert bot.sent == []
    assert db.get_captcha(CHAT, JOINER) is None


def test_a_chat_outside_the_configured_groups_is_ignored():
    bot = FakeBot()
    join(bot, chat_id=-999)
    assert bot.sent == []
    assert db.get_captcha(-999, JOINER) is None


def test_captcha_disabled_sends_nothing(monkeypatch):
    monkeypatch.setattr(config, "CAPTCHA_ENABLED", False)
    bot = FakeBot()
    join(bot)
    assert bot.sent == []
    assert db.get_captcha(CHAT, JOINER) is None


def test_leaving_is_not_a_join():
    bot = FakeBot()
    join(bot, old="member", new="left")
    assert bot.sent == []


def test_a_failed_initial_mute_does_not_create_a_challenge():
    """If we could not mute them, a challenge would be a lie."""

    class Bot(FakeBot):
        async def restrict_chat_member(self, chat_id, user_id, permissions=None, **kw):
            raise TelegramError("not enough rights")

    bot = Bot()
    join(bot)
    assert bot.sent == []
    assert db.get_captcha(CHAT, JOINER) is None


# ══ 4. Isolation ═════════════════════════════════════════════════════════
def test_two_members_in_one_group_are_independent():
    bot = FakeBot()
    join(bot, user_id=JOINER)
    join(bot, user_id=OTHER_JOINER)
    expire(user_id=JOINER)

    reap(bot)

    assert bot.banned == [(CHAT, JOINER)], "the wrong member was kicked"
    assert db.get_captcha(CHAT, OTHER_JOINER) is not None


def test_one_member_in_two_groups_is_verified_separately():
    bot = FakeBot()
    join(bot, chat_id=CHAT)
    join(bot, chat_id=OTHER_CHAT)
    assert db.get_captcha(CHAT, JOINER) is not None
    assert db.get_captcha(OTHER_CHAT, JOINER) is not None

    click(bot, chat_id=CHAT)

    assert db.get_captcha(CHAT, JOINER) is None
    assert db.get_captcha(OTHER_CHAT, JOINER) is not None, "the other group leaked"

    expire(chat_id=OTHER_CHAT)
    reap(bot)
    assert bot.banned == [(OTHER_CHAT, JOINER)]


# ══ 5. Restart and admin promotion ═══════════════════════════════════════
def test_the_challenge_survives_a_restart_and_can_still_be_solved():
    """Rows are in SQLite, so a restart mid-challenge changes nothing."""
    bot = FakeBot()
    join(bot)
    row = db.get_captcha(CHAT, JOINER)
    assert db.get_captcha(CHAT, JOINER) == row

    q = click(bot)
    assert q.last_answer == "✅ تأیید شد"
    reap(bot)
    assert bot.banned == []


def test_a_restart_does_not_extend_the_deadline():
    bot = FakeBot()
    join(bot)
    before = db.get_captcha(CHAT, JOINER)[1]
    expire()
    assert db.get_captcha(CHAT, JOINER)[1] < before


def test_a_member_promoted_during_the_challenge_is_released_not_kicked():
    """We cannot restrict an administrator, and kicking one would be worse."""
    bot = FakeBot()
    join(bot)
    expire()
    bot.admin = True
    main._admin_cache.clear()  # the join's lookup is cached for 300s

    reap(bot)

    assert bot.banned == [], "an administrator was kicked"
    assert bot.unmuted == [(CHAT, JOINER)], "the admin was left muted"
    assert db.get_captcha(CHAT, JOINER) is None


def test_a_promoted_admin_is_released_even_if_the_release_fails():
    bot = FakeBot()
    join(bot)
    expire()
    bot.admin = True
    bot.unmute_fails = True
    main._admin_cache.clear()

    reap(bot)

    assert bot.banned == []
    assert db.get_captcha(CHAT, JOINER) is None, "the reaper would retry forever"


# ══ 6. The claim primitives themselves ═══════════════════════════════════
def test_only_one_side_can_claim_a_challenge():
    db.add_captcha(CHAT, JOINER, 900, int(time.time()) + 60)
    now = int(time.time())
    assert db.claim_captcha(CHAT, JOINER, before=now) is True
    assert db.claim_captcha(CHAT, JOINER, before=now) is False
    assert db.claim_expired_captcha(CHAT, JOINER, now=now) is False


def test_an_expired_challenge_is_not_claimable_by_the_press():
    db.add_captcha(CHAT, JOINER, 900, int(time.time()) - 1)
    now = int(time.time())
    assert db.claim_captcha(CHAT, JOINER, before=now) is False
    assert db.claim_expired_captcha(CHAT, JOINER, now=now) is True
    assert db.claim_expired_captcha(CHAT, JOINER, now=now) is False


def test_a_live_challenge_is_not_claimable_by_the_reaper():
    db.add_captcha(CHAT, JOINER, 900, int(time.time()) + 60)
    assert db.claim_expired_captcha(CHAT, JOINER, now=int(time.time())) is False
    assert db.get_captcha(CHAT, JOINER) is not None


def test_the_claim_is_scoped_to_one_chat():
    db.add_captcha(CHAT, JOINER, 900, int(time.time()) + 60)
    db.add_captcha(OTHER_CHAT, JOINER, 901, int(time.time()) + 60)
    assert db.claim_captcha(CHAT, JOINER, before=int(time.time())) is True
    assert db.get_captcha(OTHER_CHAT, JOINER) is not None
