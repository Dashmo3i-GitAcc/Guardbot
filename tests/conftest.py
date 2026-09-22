"""Make the app package importable in tests without a real .env."""
import os
import sys

import pytest

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("GROUP_IDS", "-1001234567890")
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("TMP_DIR", "/tmp/guardbot-test")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def fresh_nexus_state():
    """Start every test from a known Nexus state.

    ``app/nexus.py`` caches the runtime state in a module-level variable, and
    that is deliberate — it is read on every group message and a database hit
    per message would be wasteful. The cost is that a test which switches Nexus
    off would leave every later test in a silent bot, which is the kind of
    cross-test contamination that turns a real failure into an unrelated one.

    Clearing the cache is enough: the next read comes from the database, which
    is ``:memory:`` per process and empty unless a test wrote to it.

    ``app/awareness.py`` keeps two clocks the same way — when each room's newest
    unread message arrived, and when the age purge last ran. Both are in-process
    and both are read as "how long has it been", so a value left behind by one
    test would make the next one's wait or purge depend on test order.
    """
    from app import awareness, main, nexus

    nexus.reset_state()
    awareness.reset_timers()
    # The duplicate-reply marker: a room id left behind by one test would make
    # the next test's ambient pass refuse to answer, which is exactly the kind of
    # order-dependent failure that hides a real defect.
    main._nexus_addressed.clear()
    # The bot's own Telegram rights, which are cached per chat with a TTL. A
    # test that gives its fake bot a permission would otherwise hand that
    # permission to the next test's fake bot, and the failure would read as a
    # security bug — "it muted without the right" — rather than as a cache.
    main._bot_rights_cache.clear()
    yield
    nexus.reset_state()
    awareness.reset_timers()
    main._nexus_addressed.clear()
    main._bot_rights_cache.clear()


def local_only_moderation(monkeypatch, *, threshold: float = 0.45) -> None:
    """Put the policy into the mode where the local detector may delete alone.

    Several suites test what happens *after* a confirmed deletion — the strike
    ladder, the warning, the timed restriction, the test account's auto-
    unrestrict, the admin report, the temp-file cleanup. Those are the plumbing
    that runs once something is deleted, and they need a deletion to happen.

    In the default policy mode (``MODERATION_REQUIRE_AI_CONFIRM=True``) a local
    signal never deletes, so those suites would have nothing to exercise. This
    helper therefore switches the *configuration* they run under — not the code
    they exercise — to the mode where the local detector is allowed to act.

    The default mode has its own suites: ``tests/test_mod_policy.py`` for the
    rules and ``tests/test_moderation_ai.py`` for the AI layer.
    """
    from app import config

    monkeypatch.setattr(config, "MODERATION_REQUIRE_AI_CONFIRM", False)
    monkeypatch.setattr(config, "MODERATION_LOCAL_HARD_THRESHOLD", threshold)
    # No test may reach the network, and the moderation AI is the one workload
    # that would be called on the media path.
    monkeypatch.setattr(config, "GEMINI_MOD_ENABLED", False)
