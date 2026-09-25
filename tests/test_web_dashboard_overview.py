"""M3: the Overview — real data, honest gaps, and no leaks.

The Overview is the panel's first *read* page, and these tests hold the three
properties that make it worth having:

* **It shows real state.** Every number on the page comes from a row the bot
  wrote, and the tests seed rows and assert the rendered number matches — so a
  metric that stopped reading its source would fail here rather than quietly
  render a zero.
* **It is honest about what it cannot show.** There is no persisted latency and
  no error log, and the page says so instead of inventing either. A read that
  fails degrades one number and names the source; it never blanks the page and
  never turns a failure into a confident zero.
* **It reads without writing and without leaking.** Serving the page must not
  touch a row, and it must not put a room's identity or content on a
  deployment-wide screen.

They drive the real aiohttp application over real HTTP, like the M1 and M2
suites, and point the credential store at a temp file so no test can reach the
deployment's credential.
"""
from __future__ import annotations

import contextlib
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from app import config, db
from app.web import auth, credentials, queries
from app.web.jalali import fa_digits, fa_number, format_relative
from app.web.server import create_app

PASSWORD = "correct-horse-battery"
USERNAME = "owner"
# The Telegram identity the panel is bound to. Distinct from any administrator a
# test creates, because "a Telegram admin is not a dashboard admin" is M2's rule
# and M3's page must be reached under it, not around it.
OPERATOR_ID = 424242
MODERATOR_ID = 222222

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class OverviewTestCase(unittest.IsolatedAsyncioTestCase):
    """A real app over a real HTTP client, on a known-empty set of tables."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_path = config.DASHBOARD_CREDENTIALS_PATH
        config.DASHBOARD_CREDENTIALS_PATH = os.path.join(self._tmp.name, "creds.json")
        credentials.reset_cache_for_tests()

        self._saved_auth = (
            auth.DASHBOARD_USERNAME,
            auth.DASHBOARD_PASSWORD,
            auth.DASHBOARD_PASSWORD_HASH,
            auth.DASHBOARD_SECURE_COOKIES,
        )
        auth.DASHBOARD_USERNAME = USERNAME
        auth.DASHBOARD_PASSWORD = PASSWORD
        auth.DASHBOARD_PASSWORD_HASH = ""
        auth.DASHBOARD_SECURE_COOKIES = False

        self._saved_ids = (config.OWNER_USER_ID, config.DASHBOARD_OPERATOR_ID)
        config.OWNER_USER_ID = OPERATOR_ID
        config.DASHBOARD_OPERATOR_ID = OPERATOR_ID

        auth.throttle.clear()
        self._reset_sources()

        self.app = create_app(started_at=time.monotonic() - 7200)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        (
            auth.DASHBOARD_USERNAME,
            auth.DASHBOARD_PASSWORD,
            auth.DASHBOARD_PASSWORD_HASH,
            auth.DASHBOARD_SECURE_COOKIES,
        ) = self._saved_auth
        config.OWNER_USER_ID, config.DASHBOARD_OPERATOR_ID = self._saved_ids
        auth.throttle.clear()
        self._reset_sources()
        config.DASHBOARD_CREDENTIALS_PATH = self._saved_path
        credentials.reset_cache_for_tests()
        self._tmp.cleanup()

    # ── fixtures ──────────────────────────────────────────────────────────
    @staticmethod
    def _reset_sources() -> None:
        """Empty every table the Overview reads.

        The database is ``:memory:`` and shared by the whole test session, so
        another test's rows would otherwise show up as this page's numbers. Only
        *today's* usage rows are dropped: the page reads today, and deleting
        older days would reach into rows no M3 test asserts on.
        """
        db.authorized_groups_reset()
        db.people_reset()
        db.pool_reset()
        db.nexus_state_reset()
        db.awareness_control_reset()
        db.search_control_reset()
        db.seen_updates_reset()
        db.admin_remove(MODERATOR_ID)
        day = db.ai_day()
        with db._lock:
            for table in (
                "ai_usage", "chat_usage", "moderation_usage", "transcript_usage"
            ):
                db._conn.execute(f"DELETE FROM {table} WHERE day=?", (day,))
            db._conn.commit()

    def seed_everything(self) -> None:
        """A known set of rows, so every rendered number has one right answer."""
        # Rooms: three registered, two enabled.
        db.authorized_group_set(-1001, enabled=True, title="اتاق-الف", added_by=OPERATOR_ID)
        db.authorized_group_set(-1002, enabled=True, title="اتاق-ب", added_by=OPERATOR_ID)
        db.authorized_group_set(-1003, enabled=False, title="اتاق-ج", added_by=OPERATOR_ID)

        # People: seven distinct speakers.
        for user_id in range(100, 107):
            db.people_remember(-1001, user_id, first_name=f"p{user_id}")

        # Accounts: four rows, two of them usable, one dead, one out of quota.
        db.pool_account_save(
            "chat", "slot-1", fingerprint="fp-chat-1", masked="...aaaa",
            state="ACTIVE", requests=10, successes=9, failures=1, rate_limits=1,
        )
        db.pool_account_save(
            "chat", "slot-2", fingerprint="fp-chat-2", masked="...bbbb",
            state="INVALID", requests=3, successes=0, failures=3,
        )
        db.pool_account_save(
            "awareness", "slot-1", fingerprint="fp-aw-1", masked="...cccc",
            state="ACTIVE", requests=5, successes=5, failures=0,
        )
        db.pool_account_save(
            "transcribe", "slot-1", fingerprint="fp-tr-1", masked="...dddd",
            state="QUOTA_EXHAUSTED", requests=2, successes=1, failures=1,
        )

        # Today's usage: intent 5 (1 error), chat 3, moderation 2, transcribe 3
        # (2 errors). Requests 13, errors 3.
        for _ in range(4):
            db.record_ai_attempt("relevant")
        db.record_ai_attempt("errors")
        for _ in range(3):
            db.record_chat_attempt("replies")
        for _ in range(2):
            db.record_mod_attempt("flagged")
        db.record_transcript_attempt("transcripts")
        db.record_transcript_attempt("errors")
        db.record_transcript_attempt("errors")

        # One recent pool event, and a bot heartbeat an hour old.
        db.pool_event_add(
            "chat", "account_failover", slot="slot-1", model="m1",
            reason="429", detail="next account",
        )
        with db._lock:
            db._conn.execute(
                "INSERT OR REPLACE INTO seen_updates (update_id, at) VALUES (?,?)",
                (9001, int(time.time()) - 3600),
            )
            db._conn.commit()

    async def login(self) -> None:
        response = await self.client.post(
            "/login",
            data={"username": USERNAME, "password": PASSWORD},
            allow_redirects=False,
        )
        assert response.status == 303, response.status


# ── The data layer ────────────────────────────────────────────────────────
class OverviewDataTests(OverviewTestCase):
    def test_the_payload_reads_the_real_rows(self):
        self.seed_everything()
        payload = queries.overview()

        self.assertEqual(payload["rooms"], {"enabled": 2, "total": 3})
        self.assertEqual(payload["people"], 7)
        self.assertEqual(payload["accounts"], {"total": 4, "active": 2})
        self.assertEqual(payload["today"], {"requests": 13, "errors": 3})
        self.assertGreater(payload["last_update"], 0)

        pools = {p["workload"]: p for p in payload["pools"]}
        self.assertEqual(set(pools), {"awareness", "chat", "transcribe"})
        self.assertEqual(pools["chat"]["accounts"], 2)
        self.assertEqual(pools["chat"]["active"], 1)
        self.assertEqual(pools["chat"]["invalid"], 1)
        self.assertEqual(pools["chat"]["requests"], 13)
        self.assertEqual(pools["transcribe"]["exhausted"], 1)
        self.assertEqual(pools["awareness"]["active"], 1)

        usage = {u["workload"]: u for u in payload["usage"]}
        self.assertEqual(usage["intent"]["calls"], 5)
        self.assertEqual(usage["intent"]["positive"], 4)
        self.assertEqual(usage["chat"]["calls"], 3)
        self.assertEqual(usage["transcribe"]["errors"], 2)
        self.assertEqual(payload["failed_sources"], [])

    def test_an_empty_database_is_all_zeros_and_no_false_alarm(self):
        payload = queries.overview()
        self.assertEqual(payload["rooms"], {"enabled": 0, "total": 0})
        self.assertEqual(payload["people"], 0)
        self.assertEqual(payload["accounts"], {"total": 0, "active": 0})
        self.assertEqual(payload["today"], {"requests": 0, "errors": 0})
        self.assertEqual(payload["last_update"], 0)
        self.assertEqual(payload["pools"], [])
        self.assertEqual(payload["attention"], [])
        # A workload with no accounts at all is not "down" — it is not
        # configured, and flagging it would be a warning about nothing.
        self.assertEqual(payload["failed_sources"], [])

    def test_a_pool_with_no_usable_account_is_flagged(self):
        self.seed_everything()
        attention = queries.overview()["attention"]
        kinds = {(a["kind"], a["workload"]) for a in attention}
        self.assertIn(("pool_empty", "تبدیل صدا به متن"), kinds)
        self.assertIn(("pool_invalid", "گفتگو"), kinds)
        self.assertIn(("pool_one", "گفتگو"), kinds)
        # Danger sorts before warning, so the thing to fix first is on top.
        self.assertEqual(attention[0]["level"], "danger")

    def test_a_healthy_pool_raises_nothing(self):
        db.pool_account_save("chat", "slot-1", fingerprint="a", masked="...a",
                             state="ACTIVE")
        db.pool_account_save("chat", "slot-2", fingerprint="b", masked="...b",
                             state="ACTIVE")
        self.assertEqual(queries.overview()["attention"], [])

    def test_the_buckets_mirror_the_bots_own_health_grouping(self):
        # `Pool.health` folds UNAVAILABLE into limited and counts an account
        # still inside a cooldown as limited too. The panel must agree, or the
        # two surfaces would describe one pool differently.
        db.pool_account_save("chat", "slot-1", fingerprint="a", masked="...a",
                             state="UNAVAILABLE")
        db.pool_account_save("chat", "slot-2", fingerprint="b", masked="...b",
                             state="ACTIVE", cooldown_until=int(time.time()) + 600)
        db.pool_account_save("chat", "slot-3", fingerprint="c", masked="...c",
                             state="ACTIVE", cooldown_until=int(time.time()) - 600)
        pool = {p["workload"]: p for p in queries.overview()["pools"]}["chat"]
        self.assertEqual(pool["limited"], 2)   # UNAVAILABLE + cooling
        self.assertEqual(pool["active"], 1)    # the elapsed cooldown is active
        self.assertEqual(pool["accounts"], 3)

    def test_a_failing_source_degrades_and_is_named(self):
        with mock.patch.object(
            db, "pool_accounts", side_effect=sqlite3.OperationalError("no such table")
        ):
            payload = queries.overview()
        self.assertIn("accounts", payload["failed_sources"])
        self.assertEqual(payload["pools"], [])
        self.assertEqual(payload["accounts"], {"total": 0, "active": 0})
        # The rest of the page is still there.
        self.assertEqual(len(payload["usage"]), 4)

    def test_every_source_can_fail_without_raising(self):
        breakers = ("authorized_group_list", "people_count", "pool_accounts",
                    "seen_updates_latest", "pool_events", "pool_counts",
                    "ai_usage", "chat_usage", "mod_usage", "transcript_usage",
                    "nexus_state_get", "awareness_control_get", "search_control_get")
        with contextlib.ExitStack() as stack:
            for name in breakers:
                stack.enter_context(
                    mock.patch.object(db, name, side_effect=RuntimeError("boom"))
                )
            payload = queries.overview()
        # Every source failed, and the page is still a well-formed payload that
        # names all of them rather than a traceback.
        self.assertEqual(payload["pools"], [])
        self.assertEqual(payload["attention"], [])
        for name in ("rooms", "people", "accounts", "last_update", "events"):
            self.assertIn(name, payload["failed_sources"])
        for name, _, _ in queries.USAGE_SOURCES:
            self.assertIn(f"usage:{name}", payload["failed_sources"])

    def test_reading_the_overview_writes_nothing(self):
        self.seed_everything()
        before = {
            "accounts": db.pool_accounts(),
            "counts": db.pool_counts(),
            "events": db.pool_events(50),
            "audit": db.dashboard_audit_count(),
            "groups": db.authorized_group_list(),
            "people": db.people_count(),
            "chat": db.chat_usage(),
            "intent": db.ai_usage(),
            "last": db.seen_updates_latest(),
        }
        queries.overview()
        after = {
            "accounts": db.pool_accounts(),
            "counts": db.pool_counts(),
            "events": db.pool_events(50),
            "audit": db.dashboard_audit_count(),
            "groups": db.authorized_group_list(),
            "people": db.people_count(),
            "chat": db.chat_usage(),
            "intent": db.ai_usage(),
            "last": db.seen_updates_latest(),
        }
        self.assertEqual(before, after)

    def test_the_switches_report_unset_as_unset(self):
        switches = {s["key"]: s["state"] for s in queries.overview()["switches"]}
        # Nexus defaults to online; awareness and search have never been touched.
        self.assertEqual(switches["nexus"][0], "روشن")
        self.assertEqual(switches["awareness"][0], "پیش‌فرض تنظیمات")
        self.assertEqual(switches["search"][0], "پیش‌فرض تنظیمات")

        db.awareness_control_set(False, actor_id=OPERATOR_ID, reason="test")
        switches = {s["key"]: s["state"] for s in queries.overview()["switches"]}
        self.assertEqual(switches["awareness"][0], "خاموش")


# ── The page ──────────────────────────────────────────────────────────────
class OverviewHttpTests(OverviewTestCase):
    async def test_the_overview_renders_the_real_numbers(self):
        self.seed_everything()
        await self.login()
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        body = await response.text()

        self.assertIn("نمای کلی", body)
        # The headline numbers, exactly as the rows were seeded.
        self.assertIn(f"{fa_digits(2)} / {fa_digits(3)}", body)   # rooms 2 / 3
        self.assertIn(fa_number(7), body)                          # people
        self.assertIn(f"{fa_digits(2)} / {fa_digits(4)}", body)   # accounts 2 / 4
        self.assertIn(fa_number(13), body)                         # requests
        self.assertIn(fa_number(3), body)                          # errors
        self.assertIn(format_relative(int(time.time()) - 3600), body)
        # The workload labels, the state counts and the event label.
        self.assertIn("گفتگو", body)
        self.assertIn("تبدیل صدا به متن", body)
        self.assertIn("حساب عوض شد", body)

    async def test_the_overview_renders_empty_states_when_there_is_no_data(self):
        await self.login()
        body = await (await self.client.get("/")).text()
        self.assertIn("هنوز هیچ حسابی ثبت نشده", body)
        self.assertIn("امروز هنوز هیچ درخواستی", body)
        self.assertIn("هنوز رویدادی ثبت نشده", body)
        # With nothing to warn about, the page says so rather than showing an
        # empty warnings list.
        self.assertIn("چیزی نیست که بخواد نگرانت کنه", body)

    async def test_the_overview_does_not_put_a_group_on_the_page(self):
        # The Overview is the owner's deployment-wide view. It may count rooms;
        # it may not name one, or show anything from inside one.
        self.seed_everything()
        await self.login()
        body = await (await self.client.get("/")).text()
        for title in ("اتاق-الف", "اتاق-ب", "اتاق-ج"):
            self.assertNotIn(title, body)
        self.assertNotIn("-1001", body)
        self.assertNotIn("p100", body)  # a person's name
        # The count is still there.
        self.assertIn(f"{fa_digits(2)} / {fa_digits(3)}", body)

    async def test_the_overview_states_what_it_cannot_show(self):
        await self.login()
        body = await (await self.client.get("/")).text()
        self.assertIn("چیزی که این صفحه نشون نمی‌ده", body)
        # The three honest gaps: latency is not persisted, there is no error
        # log, and the heartbeat is a proxy.
        self.assertIn("تأخیر پاسخ‌ها جایی ذخیره نمی‌شه", body)
        self.assertIn("لاگ خطایی هم به‌صورت فایل وجود نداره", body)
        self.assertIn("ضربان جدا نمی‌فرسته", body)

    async def test_a_failing_read_shows_the_partial_note_not_a_crash(self):
        self.seed_everything()
        await self.login()
        with mock.patch.object(
            db, "pool_accounts", side_effect=sqlite3.OperationalError("no such table")
        ):
            response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        body = await response.text()
        self.assertIn("ناقص‌اند", body)
        self.assertIn("accounts", body)   # the failed source is named
        self.assertIn("حساب عوض شد", body)  # the events table still rendered

    async def test_the_overview_requires_the_panel_permission(self):
        db.admin_set(
            MODERATOR_ID, "moderator", ["commands.use", "moderation.review"],
            granted_by=OPERATOR_ID, note="test",
        )
        config.DASHBOARD_OPERATOR_ID = MODERATOR_ID
        await self.login()
        response = await self.client.get("/")
        self.assertEqual(response.status, 403)
        self.assertIn("اجازه‌ش رو نداره", await response.text())

    async def test_the_nav_marks_the_overview_as_the_current_page(self):
        await self.login()
        body = await (await self.client.get("/")).text()
        self.assertIn('aria-current="page"', body)
        self.assertIn("نمای کلی", body)
        # The M1 placeholder is gone.
        self.assertNotIn("پنل بالاست", body)


# ── The architecture the page depends on ──────────────────────────────────
class ArchitectureTests(unittest.TestCase):
    def test_a_stored_epoch_renders_as_a_relative_time(self):
        # The panel's own timestamp columns (`gemini_events.at`,
        # `seen_updates.at`, `dashboard_audit.at`) are integer epochs, not
        # SQLite's `CURRENT_TIMESTAMP` strings. `_parse` has to understand both,
        # or every "when" on the page silently renders as an em dash.
        now = int(time.time())
        self.assertEqual(format_relative(now - 3600), "۱ ساعت پیش")
        self.assertEqual(format_relative(now), "همین حالا")
        # A bool is an int in Python; it must not become 1970.
        from app.web.jalali import _parse
        self.assertIsNone(_parse(True))

    def test_reading_the_overview_does_not_build_the_bots_pools(self):
        # The panel is a second process. If its data layer imported
        # `app.gemini_pool`, it would build a *second* pool registry from the
        # panel's own environment and report that as the bot's state — a whole
        # class of wrong number that no row-level test would catch. So the guard
        # is on the import graph itself, checked in a fresh interpreter because
        # the test session has already imported everything.
        code = (
            "import sys\n"
            "import app.web.queries\n"
            "heavy = [m for m in ("
            "'app.gemini_pool', 'app.chat', 'app.admin_service', 'app.nexus',"
            "'app.awareness', 'app.main') if m in sys.modules]\n"
            "print(','.join(heavy))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "", "the panel built the bot's modules")

    def test_the_overview_never_calls_a_writer(self):
        # A source-level check, because a reader that happens to write is the
        # one failure a behavioural test on a happy path would miss. The data
        # layer may name only readers; any `_write`/`_set`/`_add`/`_reset`/`_save`
        # call in it is a bug.
        source = (REPO_ROOT / "app" / "web" / "queries.py").read_text()
        for verb in ("_write(", "_set(", "_add(", "_reset(", "_save(", "_prune("):
            self.assertNotIn(verb, source, f"queries.py calls a writer: {verb}")


if __name__ == "__main__":
    unittest.main()
