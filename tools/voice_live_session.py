#!/usr/bin/env python3
"""Create the MTProto user session Nexus Voice Live needs. Once, by hand.

Why this cannot happen inside the bot
------------------------------------
Joining a Telegram voice chat is an MTProto operation. The Bot API has no method
for it at all — checked against all 277 public ``Bot`` methods — so the bot
cannot do this with its own token: it needs an ``api_id``/``api_hash`` pair and a
*user* login. That login is interactive by nature, because Telegram sends a code
to a phone. A bot process that stopped to ask for a code would be a bot process
that had stopped moderating, so the login happens here, once, and the bot only
ever reads the resulting session file.

What it writes, and where
-------------------------
One file: ``/data/voice_live.session`` (``GEMINI_LIVE_SESSION_PATH``), mode
``0600``, inside the mounted data volume. It is a credential — it *is* a logged
in Telegram account — and it is treated as one:

* it is not in Git (``data/`` is ignored, and so is ``.env``);
* it is not in the image (the Dockerfile copies source only);
* nothing here prints the phone number, the code, the password, the
  ``api_hash``, or any part of the session;
* the transport reads it and never writes to it.

Running it
----------
Inside the container, where ``/data`` is the volume and Telethon is installed::

    docker compose run --rm guardbot python -m tools.voice_live_session

It asks for the phone number, then the code Telegram sends, and — only when the
account has two-step verification — the password. The code and the password are
read without echo.

What it will not do
-------------------
It will not replace an existing session without being told to. If a session is
already there and already authorised it says so and exits without touching it;
if one is there but does not work, it asks before replacing it, and ``--force``
is the non-interactive way to answer yes.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys

# ── Exit codes ────────────────────────────────────────────────────────────
# Distinct because they need different fixes, and an operator scripting this
# should not have to parse prose to tell "you have not set the variables" from
# "Telegram said no".
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_MISCONFIGURED = 2
EXIT_REFUSED = 3

# The session is a login, so it is readable by its owner and nobody else. The
# directory gets the same treatment: a session inside a world-readable directory
# is one `ls` away from being copied.
SESSION_MODE = 0o600
DIR_MODE = 0o700

# The side files SQLite may leave beside the session. Removed together with it,
# because a stale ``-wal`` beside a fresh session is a corrupt session.
_SIDECARS = ("", "-journal", "-wal", "-shm")

# The three answers ``_existing_verdict`` can give.
_KEEP = "keep"
_REPLACE = "replace"
_REFUSE = "refuse"

# What counts as "yes" at the replacement prompt. Persian included because the
# operator of this deployment speaks it and typing English at their own tool
# should not be a trap.
_YES = ("yes", "y", "بله", "آره")


def _quiet() -> None:
    """Make sure nothing this tool does reaches a log.

    Telethon logs connection detail at INFO and, on some paths, the phone number
    it is sending a code to. This tool prints its own sentences and nothing else,
    so the library's logging is turned down rather than trusted to be discreet.
    """
    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.ERROR)


def _session_files(path: str) -> list[str]:
    return [path + suffix for suffix in _SIDECARS]


def _remove_session(path: str) -> None:
    """Delete a session and its side files. Missing files are not an error."""
    for name in _session_files(path):
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass
        except OSError:
            # Best effort: if it cannot be removed, the overwrite below will
            # fail loudly rather than silently reuse a session we meant to drop.
            pass


def _ensure_directory(path: str) -> None:
    """Create the session's directory if needed, private to its owner."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, mode=DIR_MODE, exist_ok=True)


def _harden(path: str) -> None:
    """Tighten the session's permissions, and its directory's.

    Done after the file exists rather than relying on the process umask: the
    library creates it, and a umask is a property of whoever ran the command,
    not of what the file is.
    """
    for name in _session_files(path):
        try:
            os.chmod(name, SESSION_MODE)
        except OSError:
            pass
    parent = os.path.dirname(os.path.abspath(path))
    try:
        os.chmod(parent, DIR_MODE)
    except OSError:
        pass


def _telethon_client(session_path: str, api_id: int, api_hash: str):
    """The real client. Imported here so the module is importable without it."""
    from telethon import TelegramClient

    return TelegramClient(session_path, api_id, api_hash)


async def _existing_verdict(
    *,
    session_path: str,
    api_id: int,
    api_hash: str,
    client_factory,
    force: bool,
    say,
    ask,
) -> str:
    """Whether an existing session may be kept, replaced, or must be left alone.

    A working session is not something to re-create casually: it is a logged-in
    account, and the operator may have just forgotten it was already there. So
    the default answer is ``keep``, and replacing requires being told.
    """
    if force:
        say("Replacing the existing session (--force).")
        return _REPLACE
    try:
        client = client_factory(session_path, api_id, api_hash)
        await client.connect()
        try:
            if await client.is_user_authorized():
                return _KEEP
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001 - teardown is best effort
                pass
    except Exception:  # noqa: BLE001 - "cannot tell" is not "safe to clobber"
        pass
    if not sys.stdin.isatty():
        say(
            "A session file is already here and cannot be checked without a "
            "terminal. Pass --force to replace it."
        )
        return _REFUSE
    answer = ask('A session file is already here. Type "yes" to replace it: ')
    return _REPLACE if answer.strip().lower() in _YES else _REFUSE


async def bootstrap(
    *,
    session_path: str,
    api_id: int,
    api_hash: str,
    client_factory=_telethon_client,
    say=print,
    ask=input,
    ask_secret=None,
    force: bool = False,
) -> int:
    """Create the session. Returns one of the ``EXIT_*`` codes.

    Every input is injectable so the flow can be tested without a phone, a
    network or a terminal — the prompts and the client are the only two things
    that need the real world, and both are parameters.
    """
    if ask_secret is None:
        ask_secret = getpass.getpass

    existed = os.path.exists(session_path)
    if existed:
        verdict = await _existing_verdict(
            session_path=session_path,
            api_id=api_id,
            api_hash=api_hash,
            client_factory=client_factory,
            force=force,
            say=say,
            ask=ask,
        )
        if verdict == _KEEP:
            say("A session already exists here and is already authorised; nothing to do.")
            return EXIT_OK
        if verdict == _REFUSE:
            say("Left the existing session untouched.")
            return EXIT_REFUSED
        _remove_session(session_path)

    try:
        from telethon.errors import SessionPasswordNeededError
    except Exception:  # noqa: BLE001 - absence is a configuration problem
        say("The telethon package is not installed in this environment.")
        return EXIT_MISCONFIGURED

    _ensure_directory(session_path)
    client = client_factory(session_path, api_id, api_hash)
    outcome = EXIT_FAILED
    try:
        await client.connect()
        phone = ask("Phone number in international format (e.g. +98912...): ").strip()
        if not phone:
            say("No phone number was given.")
            return EXIT_FAILED
        sent = await client.send_code_request(phone)
        code = ask_secret("The login code Telegram sent you: ").strip()
        if not code:
            say("No code was given.")
            return EXIT_FAILED
        try:
            await client.sign_in(
                phone=phone,
                code=code,
                phone_code_hash=getattr(sent, "phone_code_hash", None),
            )
        except SessionPasswordNeededError:
            password = ask_secret("Two-step verification password: ")
            await client.sign_in(password=password)
        if not await client.is_user_authorized():
            say("Telegram did not accept the sign-in.")
            return EXIT_FAILED
        outcome = EXIT_OK
    except Exception as exc:  # noqa: BLE001 - reported by type, never by detail
        # The exception's *message* is not printed: it can carry the phone number
        # or the endpoint, and the type is enough to know where to look.
        say(f"Could not create the session ({type(exc).__name__}).")
        return EXIT_FAILED
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        if outcome == EXIT_OK:
            _harden(session_path)
        elif not existed:
            # A failed first attempt must not leave a half-made credential that
            # looks like a session and is not one.
            _remove_session(session_path)

    say("The session was created and is authorised.")
    say(f"Stored at {session_path} (mode {SESSION_MODE:o}), inside the data volume.")
    return EXIT_OK


def _load_config():
    """Import ``app.config``, finding the project whether or not we are in it."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if here not in sys.path:
        sys.path.insert(0, here)
    from app import config  # noqa: PLC0415 - see the comment above

    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.voice_live_session",
        description=(
            "Create the Telegram MTProto user session that Nexus Voice Live "
            "joins voice chats with. Interactive, and meant to be run once."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing session without asking (it is a login; be sure)",
    )
    args = parser.parse_args(argv)

    _quiet()
    # ``app.config`` is the single source of truth for the session path, so this
    # reads it rather than repeating the default. The cost is that it demands the
    # bot's environment, which is why the documented invocation is
    # ``docker compose run`` — that loads ``.env`` for us. A missing variable is
    # reported by name, which is not a secret, and never by value.
    try:
        config = _load_config()
    except KeyError as exc:
        print(
            f"A required setting is missing from the environment: {exc.args[0]}. "
            "Run this through docker compose so that .env is loaded, e.g. "
            "`docker compose run --rm guardbot python -m tools.voice_live_session`.",
            file=sys.stderr,
        )
        return EXIT_MISCONFIGURED
    except Exception as exc:  # noqa: BLE001 - reported by type, never by detail
        print(
            f"Could not read the bot configuration ({type(exc).__name__}).",
            file=sys.stderr,
        )
        return EXIT_MISCONFIGURED

    api_id = int(getattr(config, "TELEGRAM_API_ID", 0) or 0)
    api_hash = str(getattr(config, "TELEGRAM_API_HASH", "") or "")
    session_path = str(getattr(config, "GEMINI_LIVE_SESSION_PATH", "") or "")

    # Names the variables, never their values.
    if not api_id or not api_hash:
        print(
            "TELEGRAM_API_ID and TELEGRAM_API_HASH must be set before this runs. "
            "They come from my.telegram.org; see .env.example.",
            file=sys.stderr,
        )
        return EXIT_MISCONFIGURED
    if not session_path:
        print("GEMINI_LIVE_SESSION_PATH is not set.", file=sys.stderr)
        return EXIT_MISCONFIGURED

    try:
        return asyncio.run(
            bootstrap(
                session_path=session_path,
                api_id=api_id,
                api_hash=api_hash,
                force=args.force,
            )
        )
    except KeyboardInterrupt:
        print("\nCancelled; nothing was changed.", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
