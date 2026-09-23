"""The one-time MTProto session bootstrap.

The session it creates is a credential — it *is* a logged-in Telegram account —
so the properties worth testing are not "does it work" but "what does it refuse
to do" and "what does it never say":

* it never replaces a working session without being told to;
* it never prints the phone number, the code, the password or the hash;
* it never reports an exception's *message*, which can carry the phone number;
* it never leaves a half-made session behind after a failed attempt.

The client and the three prompts are injected, so none of this needs a phone, a
network or a terminal. The client is a small stand-in rather than a mock: the
flow calls five methods on it, and naming them here is what makes the test read
like the real interaction.
"""
from __future__ import annotations

import asyncio
import os
import stat

import pytest

from tools import voice_live_session as VLS

PHONE = "+989120000000"
CODE = "12345"
PASSWORD = "hunter2"
HASH = "not-a-real-hash"


class _Sent:
    phone_code_hash = "the-code-hash"


class _FakeClient:
    """The smallest thing that behaves like a Telethon client here."""

    def __init__(
        self,
        path,
        api_id,
        api_hash,
        *,
        authorised=False,
        needs_password=False,
        boom="",
        creates_file=True,
    ):
        self.path = path
        self.authorised = authorised
        self.needs_password = needs_password
        self.boom = boom
        self.actions = []
        self.sign_ins = []
        if creates_file:
            # Telethon creates the session file when it connects. Doing it here
            # means the permission and cleanup assertions are about real files.
            VLS._ensure_directory(path)
            with open(path, "a"):
                pass

    async def connect(self):
        self.actions.append("connect")
        if self.boom == "connect":
            # The message deliberately contains things that must not be printed.
            raise RuntimeError(f"could not reach the network for {PHONE}")

    async def disconnect(self):
        self.actions.append("disconnect")

    async def is_user_authorized(self):
        return self.authorised

    async def send_code_request(self, phone):
        self.actions.append("send_code")
        return _Sent()

    async def sign_in(self, **kwargs):
        self.actions.append("sign_in")
        self.sign_ins.append(kwargs)
        if self.boom == "sign_in":
            raise RuntimeError(f"sign-in failed for {PHONE} with code {CODE}")
        if "code" in kwargs and self.needs_password:
            from telethon.errors import SessionPasswordNeededError

            raise SessionPasswordNeededError(request=None)
        self.authorised = True


class _Factory:
    """Builds the stand-in, remembering every one it built."""

    def __init__(self, **settings):
        self.settings = settings
        self.clients = []

    def __call__(self, path, api_id, api_hash):
        client = _FakeClient(path, api_id, api_hash, **self.settings)
        self.clients.append(client)
        return client


def _run(path, factory, *, ask=None, ask_secret=None, force=False):
    said = []
    code = asyncio.run(
        VLS.bootstrap(
            session_path=str(path),
            api_id=12345,
            api_hash=HASH,
            client_factory=factory,
            say=said.append,
            ask=ask or (lambda prompt: PHONE),
            ask_secret=ask_secret or (lambda prompt: CODE),
            force=force,
        )
    )
    return code, said


def _responder(*, confirm="yes", phone=PHONE):
    """Answers each prompt by what it is asking, not by the order it is asked."""

    def ask(prompt):
        return confirm if "replace" in prompt.lower() else phone

    return ask


class _NotATty:
    def isatty(self):
        return False


class _ATty:
    """A terminal, for the tests that exercise the confirmation prompt.

    pytest replaces stdin with an object that is not a tty, and the tool
    deliberately refuses to replace a session without one — so a test that wants
    to answer the prompt has to say that a terminal is present. That refusal is
    itself asserted, in the non-tty test below.
    """

    def isatty(self):
        return True


# ══ It creates the session ════════════════════════════════════════════════
def test_it_creates_the_session_and_says_so(tmp_path):
    path = tmp_path / "voice_live.session"
    code, said = _run(path, _Factory())
    assert code == VLS.EXIT_OK
    assert path.exists()
    assert "created and is authorised" in " ".join(said)


def test_the_session_is_readable_only_by_its_owner(tmp_path):
    """A session in a world-readable directory is one `ls` away from being
    copied, so both the file and its directory are asserted."""
    path = tmp_path / "sub" / "voice_live.session"
    code, _ = _run(path, _Factory())
    assert code == VLS.EXIT_OK
    assert stat.S_IMODE(os.stat(path).st_mode) == VLS.SESSION_MODE
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == VLS.DIR_MODE


def test_the_code_is_passed_to_telegram_with_its_hash(tmp_path):
    """The flow is the real three-step one: ask for a code, then sign in with
    the code *and* the hash Telegram returned with it. Dropping the hash would
    look like it worked in a test and fail against Telegram."""
    path = tmp_path / "voice_live.session"
    factory = _Factory()
    code, _ = _run(path, factory)
    assert code == VLS.EXIT_OK
    client = factory.clients[-1]
    assert client.actions == ["connect", "send_code", "sign_in", "disconnect"]
    assert client.sign_ins[0]["code"] == CODE
    assert client.sign_ins[0]["phone_code_hash"] == _Sent.phone_code_hash


# ══ It never says a credential ════════════════════════════════════════════
def test_nothing_secret_is_ever_printed(tmp_path):
    path = tmp_path / "voice_live.session"
    code, said = _run(path, _Factory())
    assert code == VLS.EXIT_OK
    text = " ".join(said)
    for secret in (PHONE, CODE, HASH, PASSWORD):
        assert secret not in text, f"the tool printed {secret!r}"


def test_a_failure_never_prints_the_exception_message(tmp_path):
    """The exception's message is where the phone number ends up. Only the type
    is reported, which is enough to know where to look."""
    path = tmp_path / "voice_live.session"
    code, said = _run(path, _Factory(boom="sign_in"))
    assert code == VLS.EXIT_FAILED
    text = " ".join(said)
    assert "RuntimeError" in text
    assert PHONE not in text
    assert CODE not in text


# ══ It does not clobber a session ═════════════════════════════════════════
def test_a_working_session_is_left_alone(tmp_path):
    """The safest default: a session that is already authorised is a logged-in
    account, and re-creating it is not something to do on a guess."""
    path = tmp_path / "voice_live.session"
    path.write_bytes(b"a working session")

    def explode(prompt):  # pragma: no cover - reached only on a regression
        raise AssertionError("it must not ask anything about a working session")

    code, said = _run(path, _Factory(authorised=True), ask=explode)
    assert code == VLS.EXIT_OK
    assert path.read_bytes() == b"a working session"
    assert "already authorised" in " ".join(said)


def test_it_will_not_replace_an_existing_session_without_confirmation(tmp_path, monkeypatch):
    path = tmp_path / "voice_live.session"
    path.write_bytes(b"an existing session")
    monkeypatch.setattr(VLS.sys, "stdin", _NotATty())

    code, said = _run(path, _Factory())
    assert code == VLS.EXIT_REFUSED
    assert path.read_bytes() == b"an existing session"
    assert "untouched" in " ".join(said)


def test_it_replaces_the_session_when_told_to(tmp_path, monkeypatch):
    monkeypatch.setattr(VLS.sys, "stdin", _ATty())
    path = tmp_path / "voice_live.session"
    path.write_bytes(b"a stale session")
    code, _ = _run(path, _Factory(), ask=_responder(confirm="yes"))
    assert code == VLS.EXIT_OK


def test_saying_no_leaves_the_session_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(VLS.sys, "stdin", _ATty())
    path = tmp_path / "voice_live.session"
    path.write_bytes(b"a stale session")
    code, _ = _run(path, _Factory(), ask=_responder(confirm="no"))
    assert code == VLS.EXIT_REFUSED
    assert path.read_bytes() == b"a stale session"


def test_an_unrecognised_answer_is_a_no(tmp_path, monkeypatch):
    """Anything that is not an explicit yes leaves the session alone. The prompt
    guards a logged-in account, so the safe reading of "maybe later" is no."""
    monkeypatch.setattr(VLS.sys, "stdin", _ATty())
    path = tmp_path / "voice_live.session"
    path.write_bytes(b"a stale session")
    code, _ = _run(path, _Factory(), ask=_responder(confirm="maybe later"))
    assert code == VLS.EXIT_REFUSED
    assert path.read_bytes() == b"a stale session"


def test_force_replaces_without_asking(tmp_path):
    path = tmp_path / "voice_live.session"
    path.write_bytes(b"a stale session")

    def ask(prompt):
        assert "replace" not in prompt.lower(), "--force must not ask about replacing"
        return PHONE

    code, _ = _run(path, _Factory(), ask=ask, force=True)
    assert code == VLS.EXIT_OK


def test_replacing_a_session_removes_its_side_files(tmp_path, monkeypatch):
    """A stale ``-wal`` beside a fresh session is a corrupt session."""
    monkeypatch.setattr(VLS.sys, "stdin", _ATty())
    path = tmp_path / "voice_live.session"
    path.write_bytes(b"a stale session")
    sidecars = [tmp_path / ("voice_live.session" + suffix) for suffix in ("-journal", "-wal", "-shm")]
    for sidecar in sidecars:
        sidecar.write_bytes(b"stale")

    code, _ = _run(path, _Factory(), ask=_responder())
    assert code == VLS.EXIT_OK
    for sidecar in sidecars:
        assert not sidecar.exists(), sidecar


# ══ Failure leaves nothing behind ═════════════════════════════════════════
def test_a_failed_attempt_leaves_no_half_made_session(tmp_path):
    """A file that looks like a session and is not one is worse than no file:
    the transport would find it, try it, and report a confusing reason."""
    path = tmp_path / "voice_live.session"
    code, _ = _run(path, _Factory(boom="sign_in"))
    assert code == VLS.EXIT_FAILED
    assert not path.exists()


def test_a_failed_attempt_does_not_delete_a_session_it_did_not_create(tmp_path, monkeypatch):
    """The cleanup above must not become a way to lose a working session: it
    only removes what this run created."""
    path = tmp_path / "voice_live.session"
    path.write_bytes(b"a stale but pre-existing session")
    monkeypatch.setattr(VLS.sys, "stdin", _NotATty())

    # Refused before anything was created, so the file survives untouched.
    code, _ = _run(path, _Factory(boom="sign_in"))
    assert code == VLS.EXIT_REFUSED
    assert path.read_bytes() == b"a stale but pre-existing session"


# ══ Two-step verification ═════════════════════════════════════════════════
def test_the_password_is_asked_for_only_when_telegram_demands_it(tmp_path):
    pytest.importorskip("telethon.errors")
    path = tmp_path / "voice_live.session"
    factory = _Factory(needs_password=True)
    prompts = []

    def ask_secret(prompt):
        prompts.append(prompt)
        return CODE if "code" in prompt.lower() else PASSWORD

    code, _ = _run(path, factory, ask_secret=ask_secret)
    assert code == VLS.EXIT_OK
    # Two secrets asked for, and the second one is the password.
    assert len(prompts) == 2
    assert "password" in prompts[1].lower()
    client = factory.clients[-1]
    assert [sorted(kw) for kw in client.sign_ins] == [["code", "phone", "phone_code_hash"], ["password"]]


def test_no_password_is_asked_for_when_it_is_not_needed(tmp_path):
    path = tmp_path / "voice_live.session"
    prompts = []

    def ask_secret(prompt):
        prompts.append(prompt)
        return CODE

    code, _ = _run(path, _Factory(), ask_secret=ask_secret)
    assert code == VLS.EXIT_OK
    assert len(prompts) == 1


# ══ main() reports a configuration problem by name ════════════════════════
def test_main_names_the_missing_variable_and_not_its_value(monkeypatch, capsys):
    class _Config:
        TELEGRAM_API_ID = 0
        TELEGRAM_API_HASH = ""
        GEMINI_LIVE_SESSION_PATH = "/data/voice_live.session"

    monkeypatch.setattr(VLS, "_load_config", lambda: _Config())
    assert VLS.main([]) == VLS.EXIT_MISCONFIGURED
    err = capsys.readouterr().err
    assert "TELEGRAM_API_ID" in err
    assert "TELEGRAM_API_HASH" in err


def test_main_reports_a_missing_bot_setting_cleanly(monkeypatch, capsys):
    """``app.config`` demands the bot's environment, so the tool has to turn that
    into a sentence rather than a traceback — and name the variable, which is not
    a secret, without printing any value."""

    def boom():
        raise KeyError("BOT_TOKEN")

    monkeypatch.setattr(VLS, "_load_config", boom)
    assert VLS.main([]) == VLS.EXIT_MISCONFIGURED
    err = capsys.readouterr().err
    assert "BOT_TOKEN" in err
    assert "docker compose" in err
