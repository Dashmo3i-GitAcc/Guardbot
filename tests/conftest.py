"""Make the app package importable in tests without a real .env."""
import os
import sys

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("GROUP_IDS", "-1001234567890")
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("TMP_DIR", "/tmp/guardbot-test")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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
