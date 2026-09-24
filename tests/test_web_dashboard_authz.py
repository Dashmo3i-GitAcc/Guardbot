"""M2: the panel's authorization (through ``rbac``) and its audit trail.

These tests are about two properties, and each one is a way the panel could be
wrong rather than a feature it has:

* **Who the panel is.** The dashboard authorizes exactly one identity, and that
  identity comes from configuration. A Telegram group administrator is not a
  dashboard administrator, and nothing a client sends can name its own
  authority.
* **What the panel remembers.** Every login, refusal and logout is appended to
  the panel's own trail — and that trail is not the bot's, cannot be edited, and
  cannot be grown from outside by knocking on the login form.

Like the M1 suite, these drive the real aiohttp application over real HTTP, and
every test points the credential store at a temp file. No test can read or write
the deployment's credential.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from aiohttp.web_urldispatcher import SystemRoute

from app import config, db, rbac
from app.web import audit, auth, authz, credentials
from app.web.server import create_app

PASSWORD = "correct-horse-battery"
USERNAME = "owner"

# Two distinct identities, because the whole point is that they are different
# questions: who owns the bot, and who the panel is bound to.
OWNER_ID = 111111
OTHER_ADMIN_ID = 333333
MODERATOR_ID = 222222


class PanelTestCase(unittest.IsolatedAsyncioTestCase):
    """A real app over a real HTTP client, with its own credential store."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        config.DASHBOARD_CREDENTIALS_PATH = os.path.join(
            self._tmp.name, "credentials.json"
        )
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
        config.OWNER_USER_ID = OWNER_ID
        config.DASHBOARD_OPERATOR_ID = OWNER_ID

        auth.throttle.clear()
        audit.reset_state()
        db.dashboard_audit_reset()
        db.admin_remove(OTHER_ADMIN_ID)
        db.admin_remove(MODERATOR_ID)

        self.client = await self._client_for(create_app())

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
        audit.reset_state()
        db.admin_remove(OTHER_ADMIN_ID)
        db.admin_remove(MODERATOR_ID)
        credentials.reset_cache_for_tests()
        self._tmp.cleanup()

    async def _client_for(self, app) -> TestClient:
        client = TestClient(TestServer(app))
        await client.start_server()
        return client

    # ── helpers ───────────────────────────────────────────────────────────
    async def login(self, client=None, password: str = PASSWORD):
        client = client or self.client
        return await client.post(
            "/login",
            data={"username": USERNAME, "password": password},
            allow_redirects=False,
        )

    def session_token(self, client=None) -> str:
        client = client or self.client
        for cookie in client.session.cookie_jar:
            if cookie.key == auth.COOKIE_NAME:
                return cookie.value
        return ""

    def rows(self, action: str | None = None) -> list[dict]:
        rows = db.dashboard_audit_recent(100)
        if action is None:
            return rows
        return [r for r in rows if r["action"] == action]

    def make_operator(self, user_id: int, role: str) -> None:
        """Make somebody a Telegram administrator *and* bind the panel to them.

        Both steps are always explicit: the first never implies the second, which
        is the property these tests exist to hold.
        """
        db.admin_set(
            user_id,
            role,
            rbac.ROLE_PERMISSIONS[role],
            granted_by=OWNER_ID,
            note="test",
        )
        config.DASHBOARD_OPERATOR_ID = user_id


# ── Who the panel is ──────────────────────────────────────────────────────
class IdentityTests(PanelTestCase):
    async def test_the_session_records_the_bound_operator(self):
        await self.login()
        session = auth.read_session(self.session_token())
        self.assertIsNotNone(session)
        self.assertEqual(session["pid"], OWNER_ID)

    async def test_the_panel_resolves_the_operator_through_rbac(self):
        await self.login()
        actor = authz.principal(auth.read_session(self.session_token()))
        self.assertEqual(actor.user_id, OWNER_ID)
        self.assertEqual(actor.role, rbac.ROLE_OWNER)
        self.assertEqual(actor.source, "owner")

    async def test_a_telegram_administrator_is_not_a_panel_operator(self):
        # A senior admin in the bot's own table — with `config.manage`, the very
        # permission the panel's pages require. Membership is not the question.
        db.admin_set(
            OTHER_ADMIN_ID,
            rbac.ROLE_SENIOR_ADMIN,
            rbac.ROLE_PERMISSIONS[rbac.ROLE_SENIOR_ADMIN],
            granted_by=OWNER_ID,
            note="test",
        )
        other = rbac.resolve(OTHER_ADMIN_ID)
        self.assertTrue(other.can(authz.PANEL_PERMISSION))
        self.assertEqual(other.source, "database")

        # The panel is bound to the owner, so a session carrying the other id is
        # not a session. This is the whole separation, and it is one comparison
        # against configuration rather than a rule about roles.
        self.assertNotEqual(auth.operator_id(), OTHER_ADMIN_ID)
        token, _ = auth.create_session()
        forged = dict(auth.read_session(token), pid=OTHER_ADMIN_ID)
        self.assertIsNone(auth.read_session(self._reissue(forged)))

    async def test_changing_the_operator_id_retires_the_old_sessions(self):
        token, _ = auth.create_session()
        self.assertIsNotNone(auth.read_session(token))
        config.DASHBOARD_OPERATOR_ID = MODERATOR_ID
        # The bound id moved, so the session minted under the old one is dead —
        # the same shape as the password epoch, for the same reason.
        self.assertIsNone(auth.read_session(token))

    async def test_a_client_cannot_name_its_own_authority(self):
        # The actor is derived from the session alone. A request that carries an
        # identity anywhere — query, header or form — is ignored, and the audit
        # row names the real actor.
        self.make_operator(MODERATOR_ID, rbac.ROLE_MODERATOR)
        await self.login()
        db.dashboard_audit_reset()

        response = await self.client.get(
            f"/?actor_id={OWNER_ID}&pid={OWNER_ID}&role=owner",
            headers={"X-Actor-Id": str(OWNER_ID), "X-Dashboard-Role": "owner"},
        )
        self.assertEqual(response.status, 403)
        refused = self.rows(audit.ACTION_AUTHZ_REFUSED)
        self.assertEqual(len(refused), 1)
        # The moderator, not the owner the request claimed to be.
        self.assertEqual(refused[0]["actor_id"], MODERATOR_ID)
        self.assertEqual(refused[0]["role"], rbac.ROLE_MODERATOR)

    async def test_principal_reads_only_the_bound_id(self):
        # A payload with extra fields cannot grant anything: `principal` looks at
        # `pid` and nothing else.
        actor = authz.principal(
            {
                "pid": MODERATOR_ID,
                "u": USERNAME,
                "role": rbac.ROLE_OWNER,
                "permissions": sorted(rbac.PERMISSION_SET),
                "actor_id": OWNER_ID,
            }
        )
        self.assertEqual(actor.user_id, MODERATOR_ID)
        self.assertEqual(actor.source, "none")  # a guest: no such administrator
        self.assertEqual(actor.permissions, frozenset())

    async def test_an_unreadable_authority_is_a_refusal_not_a_crash(self):
        # The panel opens the database but never creates the bot's tables, so on
        # a host where the bot has not yet migrated, resolving a non-owner
        # operator reaches for a table that is not there. Found by a container
        # smoke test: `POST /login` answered 500 because the audit row asked for
        # the operator's role. An authority that cannot be read is no authority —
        # the same rule `rbac.resolve_many` already states — so the panel must
        # refuse, not fall over.
        self.make_operator(MODERATOR_ID, rbac.ROLE_MODERATOR)
        with mock.patch.object(
            db, "admin_get", side_effect=sqlite3.OperationalError("no such table: admins")
        ):
            actor = authz.principal({"pid": MODERATOR_ID})
            self.assertEqual(actor.role, rbac.ROLE_GUEST)
            self.assertEqual(actor.permissions, frozenset())

            # And the end-to-end path the smoke test walked: a login that is
            # accepted, followed by a page that is refused with a reason.
            response = await self.login()
            self.assertEqual(response.status, 303)
            page = await self.client.get("/")
            self.assertEqual(page.status, 403)
            self.assertIn("اجازه‌ش رو نداره", await page.text())

    def _reissue(self, session: dict) -> str:
        """Re-sign a session payload. Only a test can do this — it needs the key."""
        import json

        payload = auth._b64(
            json.dumps(session, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        return f"{payload}.{auth._sign(payload.encode('ascii'))}"


# ── The gate ──────────────────────────────────────────────────────────────
class AuthorizationTests(PanelTestCase):
    async def test_the_home_page_requires_its_declared_permission(self):
        self.make_operator(MODERATOR_ID, rbac.ROLE_MODERATOR)
        await self.login()
        response = await self.client.get("/")
        self.assertEqual(response.status, 403)
        self.assertIn("اجازه‌ش رو نداره", await response.text())

    async def test_a_refusal_is_audited_with_the_permission_and_the_reason(self):
        self.make_operator(MODERATOR_ID, rbac.ROLE_MODERATOR)
        await self.login()
        db.dashboard_audit_reset()
        await self.client.get("/")
        refused = self.rows(audit.ACTION_AUTHZ_REFUSED)
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["permission"], authz.PANEL_PERMISSION)
        self.assertEqual(refused[0]["detail"], rbac.REASON_MISSING_PERMISSION)
        self.assertEqual(refused[0]["outcome"], audit.OUTCOME_REFUSED)

    async def test_an_owner_is_allowed(self):
        await self.login()
        self.assertEqual((await self.client.get("/")).status, 200)

    async def test_signing_out_needs_only_a_session(self):
        # The sentinel: an operator whose principal holds nothing can still leave.
        # Otherwise a misconfigured operator id would sign you in and trap you.
        self.make_operator(MODERATOR_ID, rbac.ROLE_MODERATOR)
        await self.login()
        token = self.session_token()
        csrf = auth.read_session(token)["csrf"]
        response = await self.client.post(
            "/logout", data={"csrf": csrf}, allow_redirects=False
        )
        self.assertEqual(response.status, 303)
        self.assertEqual(self.session_token(), "")

    async def test_no_owner_configured_locks_the_panel(self):
        # No owner and no operator: there is no identity to authorize at all, so
        # the panel refuses to sign anybody in rather than signing them in to a
        # wall of 403s.
        config.OWNER_USER_ID = 0
        config.DASHBOARD_OPERATOR_ID = 0
        response = await self.login()
        self.assertEqual(response.status, 503)
        self.assertEqual(self.set_cookie_headers(response), "")
        self.assertIn("DASHBOARD_OPERATOR_ID", await response.text())

    async def test_the_login_page_says_the_operator_is_missing(self):
        config.OWNER_USER_ID = 0
        config.DASHBOARD_OPERATOR_ID = 0
        body = await (await self.client.get("/login")).text()
        self.assertIn("DASHBOARD_OPERATOR_ID", body)
        # The form is hidden: accepting the password would lead nowhere.
        self.assertNotIn('name="password"', body)

    async def test_a_route_that_declares_nothing_is_refused(self):
        # Fail closed. A new page that forgets the decorator is a 403, not an
        # open door — and the route-inventory test below fails before it ships.
        app = create_app()

        async def undeclared(request: web.Request) -> web.Response:
            return web.Response(text="this must never be reached")

        app.router.add_get("/undeclared", undeclared)
        client = await self._client_for(app)
        try:
            await self.login(client)
            response = await client.get("/undeclared")
            self.assertEqual(response.status, 403)
            self.assertNotIn("this must never be reached", await response.text())
        finally:
            await client.close()

    def test_every_protected_route_declares_a_permission(self):
        checked = 0
        for route in self.client.server.app.router.routes():
            if isinstance(route, SystemRoute):
                continue
            path = route.resource.canonical
            # A prefix resource (the static directory) has a canonical with no
            # trailing slash while every real request under it has one, so the
            # public check has to see both spellings.
            if auth.is_public(path) or auth.is_public(path + "/"):
                continue
            with self.subTest(path=path):
                declared = getattr(route.handler, authz.PERMISSION_ATTR, None)
                self.assertIsNotNone(
                    declared, f"{path} is protected but declares no permission"
                )
                self.assertTrue(
                    declared == authz.AUTHENTICATED or declared in rbac.PERMISSION_SET,
                    f"{path} declares {declared!r}, which is not a real permission",
                )
            checked += 1
        # The inventory is only meaningful if it actually walked something.
        self.assertGreater(checked, 0)

    def test_the_panel_does_not_invent_its_own_permissions(self):
        # The panel's gates come from the bot's vocabulary. A permission of its
        # own would be a second authority model.
        self.assertIn(authz.PANEL_PERMISSION, rbac.PERMISSION_SET)
        self.assertNotIn(authz.AUTHENTICATED, rbac.PERMISSION_SET)

    def test_a_public_path_needs_no_permission(self):
        for path in ("/login", "/healthz", "/favicon.ico", "/static/app.css", "/static"):
            with self.subTest(path=path):
                self.assertTrue(auth.is_public(path))
        for path in ("/", "/groups", "/ai", "/staticfoo"):
            with self.subTest(path=path):
                self.assertFalse(auth.is_public(path))

    async def test_a_public_path_is_never_told_its_session_expired(self):
        # `/static` and `/static/` are answered by the static handler with a 403
        # (a directory is not a file). Because they are public, that refusal must
        # land on the error page — not be converted into "your session expired",
        # which would be a sentence about a session that never existed.
        for path in ("/static", "/static/"):
            with self.subTest(path=path):
                response = await self.client.get(path, allow_redirects=False)
                self.assertEqual(response.status, 403)
                self.assertNotIn("/login", response.headers.get("Location", ""))

    async def test_an_unauthenticated_error_page_offers_no_logout(self):
        body = await (await self.client.get("/static")).text()
        self.assertNotIn("خروج", body)

    @staticmethod
    def set_cookie_headers(response) -> str:
        return "; ".join(response.headers.getall("Set-Cookie", []))


# ── What the panel remembers ──────────────────────────────────────────────
class AuditTests(PanelTestCase):
    async def test_a_successful_login_is_audited(self):
        await self.login()
        rows = self.rows(audit.ACTION_LOGIN)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], audit.OUTCOME_OK)
        self.assertEqual(rows[0]["actor"], USERNAME)
        self.assertEqual(rows[0]["actor_id"], OWNER_ID)
        self.assertEqual(rows[0]["role"], rbac.ROLE_OWNER)
        self.assertTrue(rows[0]["client_ip"])

    async def test_a_failed_login_is_audited_without_the_password(self):
        await self.login(password="wrong-password")
        rows = self.rows(audit.ACTION_LOGIN_FAILED)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], audit.OUTCOME_REFUSED)
        self.assertEqual(rows[0]["detail"], "bad credentials")
        self.assertNotIn("wrong-password", str(rows[0]))

    async def test_the_trail_never_contains_a_password_or_a_hash(self):
        await self.login()
        await self.login(password="wrong-password")
        blob = str(db.dashboard_audit_recent(100))
        self.assertNotIn(PASSWORD, blob)
        self.assertNotIn("scrypt", blob)

    async def test_a_logout_is_audited(self):
        await self.login()
        csrf = auth.read_session(self.session_token())["csrf"]
        db.dashboard_audit_reset()
        await self.client.post("/logout", data={"csrf": csrf}, allow_redirects=False)
        rows = self.rows(audit.ACTION_LOGOUT)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor_id"], OWNER_ID)

    async def test_the_throttle_audits_one_row_per_window_not_one_per_knock(self):
        auth.throttle.max_failures = 2
        auth.throttle.window = 900
        auth.throttle.clear()
        db.dashboard_audit_reset()

        await self.login(password="nope-1")
        await self.login(password="nope-2")
        # Blocked from here on. Every one of these is refused before the password
        # is even checked, so none of them may add a row.
        for _ in range(6):
            response = await self.login()
            self.assertEqual(response.status, 429)

        self.assertEqual(len(self.rows(audit.ACTION_LOGIN_FAILED)), 2)
        self.assertEqual(len(self.rows(audit.ACTION_LOGIN_THROTTLED)), 1)
        self.assertLessEqual(db.dashboard_audit_count(), 3)

    async def test_an_unconfigured_panel_writes_nothing(self):
        # The panel is not accepting logins at all, so there is no event to
        # investigate — and a request that writes nothing is a request that cannot
        # be used to grow the table.
        auth.DASHBOARD_PASSWORD = ""
        for _ in range(5):
            self.assertEqual((await self.login()).status, 503)
        self.assertEqual(db.dashboard_audit_count(), 0)

    async def test_the_panel_trail_is_not_the_bots_trail(self):
        await self.login()
        self.assertGreater(db.dashboard_audit_count(), 0)
        # `admin_audit` — what the bot's own audit view reads — is untouched.
        self.assertEqual(db.audit_recent(50), [])

    async def test_the_trail_is_append_only(self):
        db.dashboard_audit_write("login", outcome="ok", actor="first")
        first = db.dashboard_audit_recent(1)[0]
        for _ in range(5):
            db.dashboard_audit_write("logout", outcome="ok", actor="later")
        again = [r for r in db.dashboard_audit_recent(50) if r["actor"] == "first"]
        self.assertEqual(again, [first])

    def test_there_is_no_way_to_edit_or_remove_a_row(self):
        # The trail is append-only by construction: the module offers a writer, a
        # reader, a counter and a retention sweep — and nothing that rewrites a
        # row. This is checked rather than asserted in prose.
        names = {n for n in dir(db) if n.startswith("dashboard_audit_")}
        self.assertEqual(
            names,
            {
                "dashboard_audit_write",
                "dashboard_audit_recent",
                "dashboard_audit_count",
                "dashboard_audit_prune",
                "dashboard_audit_reset",
            },
        )

    async def test_the_retention_sweep_drops_only_old_rows(self):
        db.dashboard_audit_write("login", outcome="ok", actor="old")
        db.dashboard_audit_write("login", outcome="ok", actor="new")
        # Age one row past the window by rewriting its timestamp, which only a
        # test can do — the module has no such call.
        with db._lock:
            db._conn.execute(
                "UPDATE dashboard_audit SET at = ? WHERE actor = 'old'",
                (int(time.time()) - 10 * 86400,),
            )
            db._conn.commit()
        removed = db.dashboard_audit_prune(86400)
        self.assertEqual(removed, 1)
        self.assertEqual([r["actor"] for r in db.dashboard_audit_recent(10)], ["new"])

    async def test_a_failing_audit_write_never_breaks_the_panel(self):
        with mock.patch.object(
            db, "dashboard_audit_write", side_effect=RuntimeError("disk full")
        ):
            response = await self.login()
        self.assertEqual(response.status, 303)
        self.assertTrue(self.session_token())

    async def test_the_stored_values_are_truncated(self):
        # Everything a caller supplied is bounded before it reaches a column.
        db.dashboard_audit_write(
            "x" * 500,
            outcome="y" * 500,
            actor="a" * 500,
            permission="p" * 500,
            role="r" * 500,
            detail="d" * 500,
            client_ip="i" * 500,
        )
        row = db.dashboard_audit_recent(1)[0]
        self.assertEqual(len(row["action"]), 40)
        self.assertEqual(len(row["outcome"]), 24)
        self.assertEqual(len(row["actor"]), 64)
        self.assertEqual(len(row["permission"]), 64)
        self.assertEqual(len(row["role"]), 32)
        self.assertEqual(len(row["detail"]), 300)
        self.assertEqual(len(row["client_ip"]), 64)


if __name__ == "__main__":
    unittest.main()
