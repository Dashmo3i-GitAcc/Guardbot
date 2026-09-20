"""The test account exception and the normal restriction duration.

The test account is moderated exactly like everyone else - detection, deletion,
the strike, the admin report and the real Telegram restrict call all run. The
only difference is that a *successful* restriction is lifted again after
``TEST_USER_UNRESTRICT_SECONDS`` and the warning from that cycle is removed, so
the next test violation can be sent straight away.

Everything here goes through the real ``on_media`` handler and the real
restriction helpers; only the Telegram layer and the job queue are faked.
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from telegram.error import TelegramError

from app import config, db, detector, main

CHAT_ID = -1001234567890
ADMIN_CHAT_ID = -1009999999999
TEST_USER_ID = 8299811287
NORMAL_USER_ID = 7


# --------------------------------------------------------------- fakes
class FakeFile:
    async def download_to_drive(self, path):
        with open(path, "wb") as fh:
            fh.write(b"fake-media")


class FakeJob:
    def __init__(self, callback, when, data, name):
        self.callback = callback
        self.when = when
        self.data = data
        self.name = name
        self.removed = False

    def schedule_removal(self):
        self.removed = True


class FakeJobQueue:
    """Records what was scheduled instead of running a real scheduler."""

    def __init__(self):
        self.jobs: list[FakeJob] = []

    def run_once(self, callback, when=None, data=None, name=None):
        job = FakeJob(callback, when, data, name)
        self.jobs.append(job)
        return job


class FakeBot:
    def __init__(self, restrict_fails=False, unrestrict_fails=False):
        self.sent: list[tuple[int, int, str]] = []   # chat_id, message_id, text
        self.photos: list[tuple[int, str]] = []      # chat_id, caption
        self.documents: list[tuple[int, str]] = []
        self.deleted: list[tuple[int, int]] = []     # chat_id, message_id
        self.restrict_calls: list[tuple[int, int, object, object]] = []
        self.restrict_fails = restrict_fails
        self.unrestrict_fails = unrestrict_fails
        self._next_id = 100

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="member")

    async def get_file(self, file_id):
        return FakeFile()

    async def send_message(self, chat_id, text, **kwargs):
        self._next_id += 1
        self.sent.append((chat_id, self._next_id, text))
        return SimpleNamespace(message_id=self._next_id)

    async def send_photo(self, chat_id, photo=None, caption=None, **kwargs):
        self.photos.append((chat_id, caption))

    async def send_document(self, chat_id, document=None, caption=None, **kwargs):
        self.documents.append((chat_id, caption))

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    async def restrict_chat_member(self, chat_id, user_id, permissions=None, until_date=None):
        if self.restrict_fails:
            raise TelegramError("not enough rights to restrict")
        if self.unrestrict_fails and until_date is None:
            raise TelegramError("not enough rights to unrestrict")
        self.restrict_calls.append((chat_id, user_id, permissions, until_date))


class FakeMessage:
    def __init__(self, message_id=55, **media):
        self.message_id = message_id
        self.photo = None
        self.video = None
        self.animation = None
        self.video_note = None
        self.sticker = None
        self.document = None
        for key, value in media.items():
            setattr(self, key, value)
        self.delete_calls = 0

    async def delete(self):
        self.delete_calls += 1


class StubDetector:
    def __init__(self, result=None, error=None):
        self.result = result or []
        self.error = error

    def detect(self, path):
        if self.error is not None:
            raise self.error
        return self.result


def media_obj():
    return SimpleNamespace(file_id="f", file_size=1000, thumbnail=None, thumb=None)


def explicit_raw(label="FEMALE_GENITALIA_EXPOSED", score=0.67):
    return [{"class": label, "score": score, "box": [0, 0, 1, 1]}]


def install_frames(monkeypatch, n=4):
    def frames(path, out_dir, _n):
        out = []
        for i in range(n):
            p = os.path.join(out_dir, f"f{i}.jpg")
            with open(p, "wb") as fh:
                fh.write(b"frame")
            out.append(p)
        return out

    monkeypatch.setattr(detector, "extract_frames", frames)


def send(bot, jq, user_id, message_id=1, **media):
    """Run one message through the real ``on_media`` handler."""
    msg = FakeMessage(message_id=message_id, **media)
    user = SimpleNamespace(
        id=user_id, full_name=f"User {user_id}", username=f"u{user_id}", is_bot=False
    )
    update = SimpleNamespace(
        effective_message=msg,
        effective_chat=SimpleNamespace(id=CHAT_ID),
        effective_user=user,
    )
    asyncio.run(main.on_media(update, SimpleNamespace(bot=bot, job_queue=jq)))
    return msg


def violation(bot, jq, user_id, message_id=1):
    """One confirmed explicit-media deletion (a photo)."""
    return send(bot, jq, user_id, message_id, photo=[media_obj()])


def run_job(bot, job):
    """Run a scheduled job the way the job queue would."""
    asyncio.run(job.callback(SimpleNamespace(bot=bot, job=job)))


def group_messages(bot):
    return [(mid, text) for chat_id, mid, text in bot.sent if chat_id == CHAT_ID]


def restrictions(bot):
    return [c for c in bot.restrict_calls if c[3] is not None]


def unrestricts(bot):
    return [c for c in bot.restrict_calls if c[3] is None]


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(config, "GROUP_IDS", [CHAT_ID])
    monkeypatch.setattr(config, "ADMIN_LOG_CHAT", ADMIN_CHAT_ID)
    monkeypatch.setattr(config, "TMP_DIR", str(tmp))
    monkeypatch.setattr(config, "MAX_DOWNLOAD_MB", 20)
    monkeypatch.setattr(config, "WHITELIST_USER_IDS", set())
    monkeypatch.setattr(config, "MUTE_MINUTES", 15)
    monkeypatch.setattr(config, "VIOLATION_MUTE_AFTER", 3)
    monkeypatch.setattr(config, "TEST_USER_ID", TEST_USER_ID)
    monkeypatch.setattr(config, "TEST_USER_UNRESTRICT_SECONDS", 2.0)
    monkeypatch.setattr(detector, "_scene_pipe", None)
    main._test_unrestrict_jobs.clear()
    main._test_unrestrict_notices.clear()
    main._admin_cache.clear()
    db.init()  # a fresh in-memory database for each test
    yield tmp
    main._test_unrestrict_jobs.clear()
    main._test_unrestrict_notices.clear()
    if db._conn is not None:
        db._conn.close()
        db._conn = None


# --------------------------------------------- normal restriction duration
def test_a_normal_user_is_restricted_for_fifteen_minutes(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    bot, jq = FakeBot(), FakeJobQueue()
    for i in range(3):
        violation(bot, jq, NORMAL_USER_ID, message_id=i)

    applied = restrictions(bot)
    assert len(applied) == 1
    _, user_id, permissions, until = applied[0]
    assert user_id == NORMAL_USER_ID
    assert permissions.can_send_messages is False  # the MUTED permission set
    expected = datetime.now(timezone.utc) + timedelta(minutes=15)
    assert abs((until - expected).total_seconds()) < 60


def test_the_restriction_duration_comes_from_config(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    monkeypatch.setattr(config, "MUTE_MINUTES", 1)
    bot, jq = FakeBot(), FakeJobQueue()
    for i in range(3):
        violation(bot, jq, NORMAL_USER_ID, message_id=i)

    until = restrictions(bot)[0][3]
    expected = datetime.now(timezone.utc) + timedelta(minutes=1)
    assert abs((until - expected).total_seconds()) < 60


# --------------------------------------------- the test account
def test_the_test_user_reaches_the_normal_restrict_path(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    bot, jq = FakeBot(), FakeJobQueue()
    for i in range(3):
        msg = violation(bot, jq, TEST_USER_ID, message_id=i)

    assert msg.delete_calls == 1                        # content deleted as usual
    assert db.get_strikes(CHAT_ID, TEST_USER_ID) == 3   # strikes recorded
    assert len(restrictions(bot)) == 1                  # the real restrict call
    assert restrictions(bot)[0][1] == TEST_USER_ID
    assert restrictions(bot)[0][3] is not None           # timed, not permanent


def test_the_test_user_is_not_exempt_from_detection_or_deletion(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    bot, jq = FakeBot(), FakeJobQueue()
    msg = violation(bot, jq, TEST_USER_ID, message_id=1)

    assert msg.delete_calls == 1
    assert db.get_strikes(CHAT_ID, TEST_USER_ID) == 1


def test_the_test_user_still_gets_the_admin_report(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    bot, jq = FakeBot(), FakeJobQueue()
    for i in range(3):
        violation(bot, jq, TEST_USER_ID, message_id=i)

    assert len(bot.photos) == 3                     # one report per deletion
    assert all(c == ADMIN_CHAT_ID for c, _ in bot.photos)
    assert any("حذف محتوای صریح" in (cap or "") for _, cap in bot.photos)


def test_the_unrestrict_is_scheduled_two_seconds_later(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    bot, jq = FakeBot(), FakeJobQueue()
    for i in range(3):
        violation(bot, jq, TEST_USER_ID, message_id=i)

    assert len(jq.jobs) == 1
    job = jq.jobs[0]
    assert job.when == 2.0
    assert str(TEST_USER_ID) in job.name
    assert job.data == {"chat_id": CHAT_ID, "user_id": TEST_USER_ID}


def test_the_delay_is_configurable(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    monkeypatch.setattr(config, "TEST_USER_UNRESTRICT_SECONDS", 5.0)
    bot, jq = FakeBot(), FakeJobQueue()
    for i in range(3):
        violation(bot, jq, TEST_USER_ID, message_id=i)

    assert jq.jobs[0].when == 5.0


def test_the_test_user_is_automatically_unrestricted(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    bot, jq = FakeBot(), FakeJobQueue()
    for i in range(3):
        violation(bot, jq, TEST_USER_ID, message_id=i)
    assert unrestricts(bot) == []

    run_job(bot, jq.jobs[0])

    calls = unrestricts(bot)
    assert len(calls) == 1
    chat_id, user_id, permissions, until = calls[0]
    assert (chat_id, user_id) == (CHAT_ID, TEST_USER_ID)
    assert permissions.can_send_messages is True   # FULL permissions
    assert until is None                           # not another timed restriction


def test_the_restriction_warning_is_cleaned_up_after_the_unrestrict(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    bot, jq = FakeBot(), FakeJobQueue()
    for i in range(3):
        violation(bot, jq, TEST_USER_ID, message_id=i)

    warnings = group_messages(bot)
    assert len(warnings) == 3                      # one warning per violation
    assert bot.deleted == []

    run_job(bot, jq.jobs[0])

    # the warning of the restriction cycle is removed ...
    assert (CHAT_ID, warnings[-1][0]) in bot.deleted
    # ... while the warnings of the two non-restricting violations stay
    assert (CHAT_ID, warnings[0][0]) not in bot.deleted
    assert (CHAT_ID, warnings[1][0]) not in bot.deleted


def test_the_admin_report_is_never_deleted_by_the_cleanup(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    bot, jq = FakeBot(), FakeJobQueue()
    for i in range(3):
        violation(bot, jq, TEST_USER_ID, message_id=i)

    run_job(bot, jq.jobs[0])

    # the cleanup only ever targets the group warning, never ADMIN_LOG_CHAT
    assert all(chat_id == CHAT_ID for chat_id, _ in bot.deleted)


# --------------------------------------------- no effect on other users
def test_a_different_user_does_not_get_the_two_second_behaviour(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    bot, jq = FakeBot(), FakeJobQueue()
    for i in range(3):
        violation(bot, jq, NORMAL_USER_ID, message_id=i)

    assert len(restrictions(bot)) == 1
    assert jq.jobs == []                          # nothing scheduled
    assert main._test_unrestrict_jobs == {}
    assert bot.deleted == []


def test_the_exception_can_be_disabled(monkeypatch):
    monkeypatch.setattr(config, "TEST_USER_ID", 0)
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    bot, jq = FakeBot(), FakeJobQueue()
    for i in range(3):
        violation(bot, jq, TEST_USER_ID, message_id=i)

    assert len(restrictions(bot)) == 1
    assert jq.jobs == []


# --------------------------------------------- safety
def test_a_failed_unrestrict_does_not_crash_and_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    bot, jq = FakeBot(unrestrict_fails=True), FakeJobQueue()
    for i in range(3):
        violation(bot, jq, TEST_USER_ID, message_id=i)

    with caplog.at_level("WARNING"):
        run_job(bot, jq.jobs[0])   # must not raise

    assert "TEST_UNRESTRICT_FAILED" in caplog.text
    assert unrestricts(bot) == []


def test_a_second_restriction_replaces_the_pending_job(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    bot, jq = FakeBot(), FakeJobQueue()
    for i in range(6):        # several restriction cycles
        violation(bot, jq, TEST_USER_ID, message_id=i)

    assert len(jq.jobs) > 1
    assert jq.jobs[0].removed is True     # replaced ...
    assert jq.jobs[-1].removed is False   # ... only the last one is pending
    assert len(main._test_unrestrict_jobs) == 1   # bounded: one per user


def test_a_replaced_cycle_still_cleans_up_all_of_its_warnings(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    bot, jq = FakeBot(), FakeJobQueue()
    for i in range(4):
        violation(bot, jq, TEST_USER_ID, message_id=i)

    warning_ids = [mid for mid, _ in group_messages(bot)]
    assert len(warning_ids) == 4

    run_job(bot, jq.jobs[-1])

    # both restriction cycles' warnings are removed; the two warnings from the
    # violations that did not restrict are untouched
    for mid in warning_ids[2:]:
        assert (CHAT_ID, mid) in bot.deleted
    for mid in warning_ids[:2]:
        assert (CHAT_ID, mid) not in bot.deleted


def test_a_stale_replaced_job_does_nothing(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    bot, jq = FakeBot(), FakeJobQueue()
    for i in range(6):
        violation(bot, jq, TEST_USER_ID, message_id=i)

    run_job(bot, jq.jobs[0])   # the replaced job fires late

    # a superseded job must not unrestrict early or duplicate the cleanup
    assert unrestricts(bot) == []
    assert bot.deleted == []


def test_a_failed_restrict_does_not_schedule_anything(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector(explicit_raw()))
    bot, jq = FakeBot(restrict_fails=True), FakeJobQueue()
    for i in range(3):
        violation(bot, jq, TEST_USER_ID, message_id=i)

    assert restrictions(bot) == []
    assert jq.jobs == []
    assert main._test_unrestrict_jobs == {}


# --------------------------------------------- the flood path restricts too
def test_the_test_user_exception_applies_to_the_flood_restrict(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    install_frames(monkeypatch)
    bot, jq = FakeBot(), FakeJobQueue()
    for i in range(6):        # more than BURST_MAX_ITEMS gifs
        send(bot, jq, TEST_USER_ID, i, animation=media_obj())

    # the burst fired and restricted the test user for real ...
    assert any(c[1] == TEST_USER_ID for c in restrictions(bot))
    # ... and the unrestrict was scheduled for that restriction too
    assert any(j.data["user_id"] == TEST_USER_ID for j in jq.jobs)


def test_the_flood_restrict_of_another_user_is_not_special(monkeypatch):
    monkeypatch.setattr(detector, "_detector", StubDetector([]))
    install_frames(monkeypatch)
    bot, jq = FakeBot(), FakeJobQueue()
    for i in range(6):
        send(bot, jq, NORMAL_USER_ID, i, animation=media_obj())

    assert any(c[1] == NORMAL_USER_ID for c in restrictions(bot))
    assert jq.jobs == []
