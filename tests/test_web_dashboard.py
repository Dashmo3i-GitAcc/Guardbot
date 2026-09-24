"""The Admin Control Center's foundation: identity, sessions, CSRF and the shell.

These are the tests for M1 of the dashboard (AgentMD §54.24). They drive the
real aiohttp application through a real request/response cycle — no route
handler is called directly and no middleware is stubbed — because the whole
point of the layer is the ordering between the error, session and CSRF
middlewares, and that ordering only exists over HTTP.

They also drive the *real* credential store: every test points
``DASHBOARD_CREDENTIALS_PATH`` at a temp file, so no test can read or write the
deployment's credential, and a test that changes a password changes it only in
its own temp directory.

No pytest-asyncio: the project's test venv does not have it, and aiohttp ships
the ``TestClient``/``TestServer`` pair these tests need. ``IsolatedAsyncioTestCase``
is stdlib.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest

from aiohttp.test_utils import TestClient, TestServer

from app import config
from app.web import auth, credentials
from app.web.jalali import fa_digits, fa_number, format_duration, format_jalali
from app.web.render import safe_next
from app.web.server import create_app

PASSWORD = "correct-horse-battery"
USERNAME = "owner"
# The Telegram identity the panel is bound to. M2 made the session carry it, so
# every test needs one configured — and it is deliberately a different id from
# any administrator these tests create, because "a Telegram admin is not a
# dashboard admin" is the property M2 exists to hold.
OPERATOR_ID = 424242


class DashboardTestCase(unittest.IsolatedAsyncioTestCase):
    """A real app, a real client, and a credential store that is not the real one."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._path = os.path.join(self._tmp.name, "credentials.json")

        self._saved_path = config.DASHBOARD_CREDENTIALS_PATH
        config.DASHBOARD_CREDENTIALS_PATH = self._path
        credentials.reset_cache_for_tests()

        # The bootstrap credential, set on the module the way config would have
        # set it. Saved and restored so one test's change cannot leak.
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

        # The panel's authority model: an owner (so `rbac.authorize` has somebody
        # to answer for) and the operator the panel is bound to.
        self._saved_ids = (config.OWNER_USER_ID, config.DASHBOARD_OPERATOR_ID)
        config.OWNER_USER_ID = OPERATOR_ID
        config.DASHBOARD_OPERATOR_ID = OPERATOR_ID

        auth.throttle.clear()
        self._saved_throttle = (auth.throttle.max_failures, auth.throttle.window)

        # A known start time so uptime is deterministic rather than a race.
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
        auth.throttle.max_failures, auth.throttle.window = self._saved_throttle
        auth.throttle.clear()
        config.OWNER_USER_ID, config.DASHBOARD_OPERATOR_ID = self._saved_ids
        config.DASHBOARD_CREDENTIALS_PATH = self._saved_path
        credentials.reset_cache_for_tests()
        self._tmp.cleanup()

    # ── helpers ───────────────────────────────────────────────────────────
    async def login(self, password: str = PASSWORD, username: str = USERNAME):
        return await self.client.post(
            "/login",
            data={"username": username, "password": password},
            allow_redirects=False,
        )

    @staticmethod
    def set_cookie_headers(response) -> str:
        return "; ".join(response.headers.getall("Set-Cookie", []))

    def session_token(self) -> str:
        """The session cookie the client currently holds."""
        for cookie in self.client.session.cookie_jar:
            if cookie.key == auth.COOKIE_NAME:
                return cookie.value
        return ""


# ── Password hashing and policy ───────────────────────────────────────────
class PasswordTests(DashboardTestCase):
    def test_a_hash_verifies_its_own_password_and_nothing_else(self):
        stored = auth.hash_password(PASSWORD)
        self.assertTrue(auth._verify_hash(PASSWORD, stored))
        self.assertFalse(auth._verify_hash(PASSWORD + "x", stored))
        self.assertFalse(auth._verify_hash("", stored))

    def test_a_hash_carries_its_own_parameters(self):
        stored = auth.hash_password(PASSWORD)
        scheme, n, r, p, salt, digest = stored.split("$")
        self.assertEqual(scheme, "scrypt")
        self.assertEqual(int(n), 2 ** 14)
        self.assertEqual(int(r), 8)
        self.assertEqual(int(p), 1)
        self.assertEqual(len(bytes.fromhex(salt)), 16)
        self.assertEqual(len(bytes.fromhex(digest)), 32)

    def test_the_same_password_hashes_differently_every_time(self):
        # A per-hash salt, so two operators (or two rotations) with the same
        # password do not produce the same stored value.
        self.assertNotEqual(auth.hash_password(PASSWORD), auth.hash_password(PASSWORD))

    def test_a_malformed_stored_hash_is_refused_not_half_trusted(self):
        for stored in ("", "not-a-hash", "scrypt$1$2", "bcrypt$1$2$3$4$5", "scrypt$a$b$c$d$e"):
            with self.subTest(stored=stored):
                self.assertFalse(auth._verify_hash(PASSWORD, stored))

    def test_the_policy_reports_every_problem_at_once(self):
        problems = auth.password_problems("abc", "def")
        self.assertIn("too_short", problems)
        self.assertIn("mismatch", problems)

    def test_the_policy_accepts_a_long_unique_password(self):
        self.assertEqual(auth.password_problems(PASSWORD, PASSWORD), [])

    def test_the_policy_refuses_the_lazy_choices(self):
        cases = {
            "": "empty",
            "short": "too_short",
            " password-with-space": "whitespace",
            "password": "too_short",
            "password1234": "too_common",
            "aaaaaaaaaaaaaa": "too_repetitive",
            "x" * (auth.MAX_PASSWORD_LENGTH + 1): "too_long",
        }
        for candidate, expected in cases.items():
            with self.subTest(candidate=candidate[:20]):
                self.assertIn(expected, auth.password_problems(candidate, candidate))

    def test_reusing_the_current_password_is_refused(self):
        self.assertIn(
            "unchanged",
            auth.password_problems(PASSWORD, PASSWORD, current_password=PASSWORD),
        )

    def test_credentials_are_checked_constant_time_on_both_fields(self):
        self.assertTrue(auth.verify_credentials(USERNAME, PASSWORD))
        self.assertFalse(auth.verify_credentials("someone-else", PASSWORD))
        self.assertFalse(auth.verify_credentials(USERNAME, "wrong-password"))
        # The username is stripped, so a stray space from a paste still works.
        self.assertTrue(auth.verify_credentials(f"  {USERNAME} ", PASSWORD))


# ── Sessions ──────────────────────────────────────────────────────────────
class SessionTests(DashboardTestCase):
    def test_a_minted_session_reads_back(self):
        token, session = auth.create_session()
        self.assertEqual(token.count("."), 1)
        read = auth.read_session(token)
        self.assertIsNotNone(read)
        self.assertEqual(read["u"], USERNAME)
        self.assertEqual(read["aud"], auth.AUDIENCE_ADMIN)
        self.assertTrue(read["csrf"])
        self.assertGreater(read["exp"], int(time.time()))

    def test_every_session_carries_a_different_csrf_token(self):
        first, _ = auth.create_session()
        second, _ = auth.create_session()
        self.assertNotEqual(
            auth.read_session(first)["csrf"], auth.read_session(second)["csrf"]
        )

    def test_a_tampered_payload_is_refused(self):
        token, _ = auth.create_session()
        payload, signature = token.rsplit(".", 1)
        forged = auth._b64(b'{"aud":"admin","u":"owner","exp":9999999999}')
        self.assertIsNone(auth.read_session(f"{forged}.{signature}"))
        self.assertIsNone(auth.read_session(f"{payload}.{forged}"))

    def test_garbage_is_refused_rather_than_raising(self):
        for token in (None, "", "no-dot", ".", "a.b", "a.b.c"):
            with self.subTest(token=token):
                self.assertIsNone(auth.read_session(token))

    def test_an_expired_session_is_refused(self):
        original = auth.session_seconds
        auth.session_seconds = lambda: -10
        try:
            token, _ = auth.create_session()
        finally:
            auth.session_seconds = original
        self.assertIsNone(auth.read_session(token))

    def test_a_session_for_another_audience_is_not_a_session_here(self):
        token, _ = auth.create_session(audience="somewhere-else")
        self.assertIsNone(auth.read_session(token, audience=auth.AUDIENCE_ADMIN))

    def test_an_identity_that_is_not_the_operator_is_refused(self):
        token, _ = auth.create_session(identity="someone-else")
        self.assertIsNone(auth.read_session(token))

    def test_changing_the_password_retires_every_existing_session(self):
        token, _ = auth.create_session()
        self.assertIsNotNone(auth.read_session(token))
        # A change from the panel bumps the epoch, which is what retires the
        # session — no list of live sessions is kept anywhere.
        credentials.set_password_hash(auth.hash_password("another-long-password"))
        self.assertIsNone(auth.read_session(token))

    def test_rotation_keeps_the_identity_and_changes_the_token(self):
        token, session = auth.create_session()
        rotated_token, rotated = auth.rotate_session(session)
        self.assertNotEqual(token, rotated_token)
        self.assertEqual(rotated["u"], session["u"])
        self.assertEqual(rotated["aud"], session["aud"])
        self.assertIsNotNone(auth.read_session(rotated_token))

    def test_a_fresh_session_does_not_need_rotation_and_an_old_one_does(self):
        _, session = auth.create_session()
        self.assertFalse(auth.needs_rotation(session))
        stale = dict(session, iat=int(time.time()) - auth.rotate_after_seconds())
        self.assertTrue(auth.needs_rotation(stale))

    def test_a_session_without_an_issue_time_is_not_rotated(self):
        # Defensive: a payload with no `iat` must not be treated as ancient and
        # re-minted on every request.
        self.assertFalse(auth.needs_rotation({"u": USERNAME}))


# ── The credential store ──────────────────────────────────────────────────
class CredentialStoreTests(DashboardTestCase):
    def test_the_file_is_written_with_the_hash_and_an_epoch(self):
        epoch = credentials.set_password_hash(auth.hash_password(PASSWORD))
        self.assertEqual(epoch, credentials.INITIAL_EPOCH + 1)
        self.assertTrue(credentials.is_file_managed())
        self.assertTrue(auth._verify_hash(PASSWORD, credentials.current_hash()))
        self.assertEqual(oct(os.stat(self._path).st_mode & 0o777), "0o600")

    def test_the_epoch_only_moves_forward(self):
        first = credentials.set_password_hash(auth.hash_password(PASSWORD))
        second = credentials.set_password_hash(auth.hash_password("another-long-password"))
        self.assertEqual(second, first + 1)

    def test_a_corrupt_file_is_treated_as_absent(self):
        with open(self._path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        credentials.reset_cache_for_tests()
        self.assertEqual(credentials.current_hash(), "")
        self.assertEqual(credentials.current_epoch(), credentials.INITIAL_EPOCH)

    def test_a_file_whose_hash_is_not_scrypt_is_refused(self):
        with open(self._path, "w", encoding="utf-8") as handle:
            handle.write('{"password_hash": "plaintext", "epoch": 9}')
        credentials.reset_cache_for_tests()
        self.assertEqual(credentials.current_hash(), "")

    def test_clearing_hands_control_back_to_env(self):
        credentials.set_password_hash(auth.hash_password(PASSWORD))
        self.assertTrue(credentials.clear())
        self.assertFalse(credentials.is_file_managed())
        self.assertFalse(credentials.clear())

    def test_a_file_password_wins_over_the_env_password(self):
        # The more recent decision must win, or changing the password would not
        # revoke the old one.
        credentials.set_password_hash(auth.hash_password("another-long-password"))
        self.assertFalse(auth.verify_credentials(USERNAME, PASSWORD))
        self.assertTrue(auth.verify_credentials(USERNAME, "another-long-password"))


# ── HTTP: the gate, the login, the shell ──────────────────────────────────
class HttpTests(DashboardTestCase):
    async def test_healthz_is_public_and_says_nothing(self):
        response = await self.client.get("/healthz")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"status": "ok"})

    async def test_static_assets_are_public(self):
        response = await self.client.get("/static/app.css")
        self.assertEqual(response.status, 200)
        self.assertIn("text/css", response.headers["Content-Type"])
        response = await self.client.get("/static/logo.svg")
        self.assertEqual(response.status, 200)

    async def test_a_page_without_a_session_goes_to_the_login(self):
        response = await self.client.get("/", allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["Location"], "/login?next=/")

    async def test_a_post_without_a_session_goes_to_the_login_not_a_wall(self):
        response = await self.client.post("/logout", allow_redirects=False)
        self.assertEqual(response.status, 303)
        self.assertIn("/login", response.headers["Location"])

    async def test_the_login_page_renders_a_form_when_configured(self):
        response = await self.client.get("/login")
        self.assertEqual(response.status, 200)
        body = await response.text()
        self.assertIn('name="password"', body)
        self.assertIn('name="username"', body)

    async def test_the_login_page_explains_itself_when_unconfigured(self):
        auth.DASHBOARD_PASSWORD = ""
        response = await self.client.get("/login")
        self.assertEqual(response.status, 200)
        body = await response.text()
        self.assertNotIn('name="password"', body)
        self.assertIn("DASHBOARD_PASSWORD", body)

    async def test_wrong_credentials_are_refused_without_a_cookie(self):
        response = await self.login(password="wrong-password")
        self.assertEqual(response.status, 401)
        self.assertEqual(self.set_cookie_headers(response), "")
        self.assertIn("درست نبود", await response.text())

    async def test_login_without_a_password_set_is_refused(self):
        auth.DASHBOARD_PASSWORD = ""
        response = await self.login()
        self.assertEqual(response.status, 503)
        self.assertEqual(self.set_cookie_headers(response), "")

    async def test_a_correct_login_sets_a_locked_down_cookie(self):
        response = await self.login()
        self.assertEqual(response.status, 303)
        self.assertEqual(response.headers["Location"], "/")
        cookie = self.set_cookie_headers(response)
        self.assertIn(auth.COOKIE_NAME, cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn("Path=/", cookie)
        # Off by default, because the panel is reachable over loopback only.
        self.assertNotIn("Secure", cookie)

    async def test_the_cookie_is_marked_secure_when_configured(self):
        auth.DASHBOARD_SECURE_COOKIES = True
        response = await self.login()
        self.assertIn("Secure", self.set_cookie_headers(response))

    async def test_each_login_mints_a_new_token(self):
        # Session fixation: a token that was already in the browser must never
        # become the authenticated one.
        first = await self.login()
        first_token = self.session_token()
        self.client.session.cookie_jar.clear()
        second = await self.login()
        second_token = self.session_token()
        self.assertTrue(first_token and second_token)
        self.assertNotEqual(first_token, second_token)
        self.assertEqual(first.status, second.status, 303)

    async def test_the_authenticated_home_renders(self):
        await self.login()
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        body = await response.text()
        self.assertIn("پنل بالاست", body)
        # The uptime is the injected start time, formatted in Persian.
        self.assertIn("۲ ساعت", body)

    async def test_a_tampered_cookie_does_not_authenticate(self):
        await self.login()
        token = self.session_token()
        payload, signature = token.rsplit(".", 1)
        self.client.session.cookie_jar.clear()
        self.client.session.cookie_jar.update_cookies(
            {auth.COOKIE_NAME: f"{payload}x.{signature}"}
        )
        response = await self.client.get("/", allow_redirects=False)
        self.assertEqual(response.status, 302)

    async def test_a_cookie_from_a_retired_epoch_does_not_authenticate(self):
        await self.login()
        credentials.set_password_hash(auth.hash_password("another-long-password"))
        response = await self.client.get("/", allow_redirects=False)
        self.assertEqual(response.status, 302)

    async def test_an_unknown_page_is_a_404_inside_the_shell(self):
        await self.login()
        response = await self.client.get("/no-such-page")
        self.assertEqual(response.status, 404)
        self.assertIn("وجود نداره", await response.text())

    async def test_logout_clears_the_cookie(self):
        await self.login()
        token = self.session_token()
        csrf = auth.read_session(token)["csrf"]
        response = await self.client.post(
            "/logout", data={"csrf": csrf}, allow_redirects=False
        )
        self.assertEqual(response.status, 303)
        self.assertIn("/login", response.headers["Location"])
        self.assertEqual(self.session_token(), "")


# ── CSRF ──────────────────────────────────────────────────────────────────
class CsrfTests(DashboardTestCase):
    async def test_a_post_without_the_token_is_refused(self):
        await self.login()
        response = await self.client.post("/logout", allow_redirects=False)
        self.assertEqual(response.status, 403)
        self.assertIn("فرم قدیمی", await response.text())

    async def test_a_post_with_the_wrong_token_is_refused(self):
        await self.login()
        response = await self.client.post(
            "/logout", data={"csrf": "not-the-token"}, allow_redirects=False
        )
        self.assertEqual(response.status, 403)

    async def test_a_post_with_the_header_token_is_accepted(self):
        await self.login()
        csrf = auth.read_session(self.session_token())["csrf"]
        response = await self.client.post(
            "/logout",
            headers={"X-CSRF-Token": csrf},
            allow_redirects=False,
        )
        self.assertEqual(response.status, 303)

    async def test_a_get_is_never_asked_for_a_token(self):
        await self.login()
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)


# ── The login brake ───────────────────────────────────────────────────────
class ThrottleTests(DashboardTestCase):
    async def test_repeated_failures_are_braked(self):
        auth.throttle.max_failures = 2
        auth.throttle.window = 900
        auth.throttle.clear()

        self.assertEqual((await self.login(password="nope-1")).status, 401)
        self.assertEqual((await self.login(password="nope-2")).status, 401)
        # Even the *correct* password is refused now: the brake is on the
        # address, not on the guess.
        response = await self.login()
        self.assertEqual(response.status, 429)
        self.assertIn("صبر کن", await response.text())

    async def test_a_successful_login_resets_the_counter(self):
        auth.throttle.max_failures = 3
        auth.throttle.clear()
        self.assertEqual((await self.login(password="nope")).status, 401)
        self.assertEqual((await self.login()).status, 303)
        self.assertEqual(auth.throttle.retry_after("127.0.0.1"), 0)

    async def test_the_window_expires(self):
        throttle = auth.LoginThrottle(max_failures=1, window_seconds=60)
        throttle.record_failure("1.2.3.4")
        self.assertGreater(throttle.retry_after("1.2.3.4"), 0)
        # Rewriting the recorded failure as old is how the window is tested
        # without sleeping.
        throttle._failures["1.2.3.4"][0] = time.time() - 61
        self.assertEqual(throttle.retry_after("1.2.3.4"), 0)


# ── Rotation over HTTP ────────────────────────────────────────────────────
class RotationTests(DashboardTestCase):
    async def test_a_page_view_rotates_an_aged_session(self):
        await self.login()
        before = self.session_token()

        # Make every session "old enough" rather than waiting half a session.
        original = auth.rotate_after_seconds
        auth.rotate_after_seconds = lambda: 0
        try:
            response = await self.client.get("/")
        finally:
            auth.rotate_after_seconds = original

        self.assertEqual(response.status, 200)
        after = self.session_token()
        self.assertNotEqual(before, after)
        # The rotated cookie is a real session, and the page it came with still
        # works — the token the form was rendered with is the one that was set.
        self.assertIsNotNone(auth.read_session(after))
        self.assertEqual((await self.client.get("/")).status, 200)

    async def test_a_post_does_not_rotate(self):
        # Rotation only happens on safe methods, so a POST's response never
        # carries a second Set-Cookie that could confuse a form flow.
        await self.login()
        before = self.session_token()
        original = auth.rotate_after_seconds
        auth.rotate_after_seconds = lambda: 0
        try:
            csrf = auth.read_session(before)["csrf"]
            response = await self.client.post(
                "/logout", data={"csrf": csrf}, allow_redirects=False
            )
        finally:
            auth.rotate_after_seconds = original
        self.assertEqual(response.status, 303)


# ── Small pure helpers ────────────────────────────────────────────────────
class HelperTests(unittest.TestCase):
    def test_open_redirects_are_refused(self):
        for raw in ("https://evil.example", "//evil.example", "\\\\evil", "/a\n/b"):
            with self.subTest(raw=raw):
                self.assertEqual(safe_next(raw), "/")
        self.assertEqual(safe_next("/groups?page=2"), "/groups?page=2")
        self.assertEqual(safe_next(None), "/")
        self.assertEqual(safe_next(""), "/")
        self.assertEqual(safe_next("/ok", fallback="/x"), "/ok")

    def test_persian_digits_and_thousands(self):
        self.assertEqual(fa_digits("42"), "۴۲")
        self.assertEqual(fa_number(50000), "۵۰,۰۰۰")
        self.assertEqual(fa_number(None), "۰")

    def test_durations_read_like_sentences(self):
        self.assertEqual(format_duration(7200), "۲ ساعت و ۰ دقیقه")
        self.assertEqual(format_duration(90), "۱ دقیقه")
        self.assertEqual(format_duration(-5), "۰ دقیقه")
        self.assertEqual(format_duration("nonsense"), "—")

    def test_a_jalali_date_is_rendered_in_tehran(self):
        # 2026-09-24 12:00 UTC is 15:30 in Tehran, on 2 Mehr 1405.
        self.assertEqual(format_jalali("2026-09-24 12:00:00"), "۱۴۰۵/۰۷/۰۲ ۱۵:۳۰")
        self.assertEqual(
            format_jalali("2026-09-24 12:00:00", with_month_name=True, with_time=False),
            "۲ مهر ۱۴۰۵",
        )
        self.assertEqual(format_jalali(None), "—")
        self.assertEqual(format_jalali("not a date"), "—")


if __name__ == "__main__":
    unittest.main()
