#!/usr/bin/env python3
"""Set, rotate or clear the Admin Control Center's password.

The panel reads its password from ``.env`` at process start, which is right for
a deployment file and wrong for a running service: editing ``.env`` needs a
restart, and the hash would then sit in a file the operator edits by hand. This
tool writes the credential to the runtime store instead
(``app/web/credentials.py``), where it takes effect on the next login without a
restart, and bumps the epoch so every existing session is retired.

Usage (inside the container, where the bot's environment is present)::

    docker compose exec dashboard python ops/dashboard_passwd.py --status
    docker compose exec dashboard python ops/dashboard_passwd.py --apply
    docker compose exec dashboard python ops/dashboard_passwd.py --hash
    docker compose exec dashboard python ops/dashboard_passwd.py --clear

or without the service running::

    docker run --rm -it --env-file .env -v "$PWD/data:/data" guardbot:latest \\
        python ops/dashboard_passwd.py --apply

With no ``--apply``/``--clear``/``--hash`` the tool reports status and changes
nothing. A password is never passed on the command line by default (it would
land in the shell history and in ``ps``); the prompt is hidden and the value is
never echoed, logged or written anywhere except as a scrypt hash.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

# Allow `python ops/dashboard_passwd.py` from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_app():
    """Import the app modules, or explain how to give this process an env.

    ``app/config.py`` requires ``BOT_TOKEN`` at import, exactly as the bot does.
    Inside the container that is provided by ``env_file: .env``; on the host it
    is not, so the failure is turned into the command that works rather than a
    traceback.
    """
    try:
        from app.web import auth, credentials
    except KeyError as exc:  # missing BOT_TOKEN / GROUP_IDS in the environment
        print(
            f"error: {exc} is not set, so app.config cannot be imported.\n"
            "Run this inside the container, which has the bot's environment:\n"
            "    docker compose exec dashboard python ops/dashboard_passwd.py ...\n"
            "or: docker run --rm -it --env-file .env -v \"$PWD/data:/data\" \\\n"
            "        guardbot:latest python ops/dashboard_passwd.py ...",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return auth, credentials


def _read_password(prompt: str) -> str:
    """A hidden prompt, refusing an empty answer."""
    try:
        return getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt):
        print("\naborted.", file=sys.stderr)
        raise SystemExit(1)


def cmd_status(auth, credentials) -> int:
    print(f"credential file : {credentials.path()}")
    print(f"file exists     : {'yes' if Path(credentials.path()).exists() else 'no'}")
    print(f"panel-managed   : {'yes' if credentials.is_file_managed() else 'no'}")
    print(f"epoch           : {credentials.current_epoch()}")
    if not auth.is_configured():
        source = "none — no login can succeed"
    elif credentials.is_file_managed():
        source = "the credential file (set from the panel)"
    elif auth.DASHBOARD_PASSWORD_HASH:
        source = ".env (DASHBOARD_PASSWORD_HASH)"
    else:
        source = ".env (DASHBOARD_PASSWORD)"
    print(f"password source : {source}")
    print(f"username        : {auth.DASHBOARD_USERNAME}")
    print(f"secret set      : {'yes' if not auth.secret_is_ephemeral() else 'no (sessions do not survive a restart)'}")
    # The hash, the password and the cookie secret are never printed.
    return 0


def cmd_hash(auth, credentials, password: str | None) -> int:
    candidate = password or _read_password("password to hash: ")
    problems = auth.password_problems(candidate, candidate)
    if problems:
        print(f"refused: {', '.join(problems)}", file=sys.stderr)
        return 1
    print(auth.hash_password(candidate))
    print(
        "# Put the line above in .env as DASHBOARD_PASSWORD_HASH=... and restart "
        "the dashboard.",
        file=sys.stderr,
    )
    return 0


def cmd_apply(auth, credentials, password: str | None) -> int:
    candidate = password or _read_password("new password: ")
    if not password:
        confirm = _read_password("again: ")
    else:
        # Given on the command line there is nothing to confirm against; the
        # policy below still applies.
        confirm = candidate

    problems = auth.password_problems(candidate, confirm)
    if problems:
        print(f"refused: {', '.join(problems)}", file=sys.stderr)
        return 1

    epoch = credentials.set_password_hash(auth.hash_password(candidate))
    print(f"stored. epoch is now {epoch}; every existing session is retired.")
    print("Log in again with the new password.")
    return 0


def cmd_clear(auth, credentials) -> int:
    removed = credentials.clear()
    print(
        "removed the panel-set password; .env decides again."
        if removed
        else "no panel-set password was present; .env decides."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manage the Admin Control Center password.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true", help="report the current state (default)")
    group.add_argument("--hash", nargs="?", const="", metavar="PASSWORD",
                       help="print a scrypt hash for .env (prompts if omitted)")
    group.add_argument("--apply", nargs="?", const="", metavar="PASSWORD",
                       help="store a new password (prompts if omitted)")
    group.add_argument("--clear", action="store_true", help="remove the stored password")
    args = parser.parse_args(argv)

    auth, credentials = _load_app()

    if args.clear:
        return cmd_clear(auth, credentials)
    # `nargs="?"` gives "" when the flag is present without a value (prompt) and
    # None when it is absent entirely.
    if args.hash is not None:
        return cmd_hash(auth, credentials, args.hash or None)
    if args.apply is not None:
        return cmd_apply(auth, credentials, args.apply or None)
    return cmd_status(auth, credentials)


if __name__ == "__main__":
    raise SystemExit(main())
