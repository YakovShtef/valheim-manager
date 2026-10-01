#!/usr/bin/env python3
"""Set a new admin username and password, in place, for a forgotten login.

    docker compose exec manager python tools/reset_admin.py && docker compose restart manager

Asks for the username (Enter keeps the current one) and the new password twice,
without echoing it. Then it rewrites the manager's credential file on the state
volume and nothing else: game settings, worlds, backups, the backup schedule and the
player roster are not touched. The restart is what makes the running manager read
the new login.

Everyone signed in is signed out, because the session secret is replaced too: a
forgotten or leaked password is exactly when an old session should stop working.
``--keep-sessions`` keeps the old secret instead.

If the manager will not start, so there is nothing to ``exec`` into, use
``docker compose run --rm --no-deps manager python tools/reset_admin.py`` -- the same
volumes, in a throwaway container.

Not for a manager whose login comes from ``manager.env``: there the file is what
counts, and this tool says so rather than writing credentials that would be ignored.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

# Allow running as a plain script from the repo without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.setup import HASH_ALGORITHMS, SetupInputError, admin_credentials  # noqa: E402
from app.state_store import (  # noqa: E402
    ManagerState,
    StateStore,
    StateStoreError,
    new_session_secret,
)

DEFAULT_STATE_FILE = "/srv/state/manager-state.json"
CREDENTIAL_ENV_VARS = ("ADMIN_USER", "ADMIN_PASSWORD_HASH", "SESSION_SECRET")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Set a new admin username and password for the Valheim manager."
    )
    parser.add_argument("--user", help="the new username (default: ask, Enter keeps it)")
    parser.add_argument(
        "--algo", choices=HASH_ALGORITHMS, default="bcrypt",
        help="bcrypt (default) refuses passwords over 72 bytes; argon2 has no limit",
    )
    parser.add_argument(
        "--stdin", action="store_true",
        help="read the password from one line of stdin instead of asking",
    )
    parser.add_argument(
        "--keep-sessions", action="store_true",
        help="keep the session secret, so whoever is signed in stays signed in",
    )
    parser.add_argument(
        "--state-file", default=os.environ.get("MANAGER_STATE_FILE", DEFAULT_STATE_FILE),
        help=f"the credential file (default: $MANAGER_STATE_FILE or {DEFAULT_STATE_FILE})",
    )
    args = parser.parse_args(argv)

    from_env = [name for name in CREDENTIAL_ENV_VARS if os.environ.get(name, "").strip()]
    if from_env:
        print(
            "This manager takes its login from manager.env "
            f"({', '.join(from_env)} set), so a new one written here would be ignored.\n"
            "Change ADMIN_USER and ADMIN_PASSWORD_HASH in manager.env instead; "
            "tools/hash_password.py prints the hash.",
            file=sys.stderr,
        )
        return 2

    store = StateStore(args.state_file)
    try:
        current = store.load()
    except StateStoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if current is None:
        print(
            f"There is no admin account yet ({store.path} does not exist). Finish "
            "first-run setup instead: `docker compose logs manager` prints its URL.",
            file=sys.stderr,
        )
        return 2

    user = args.user
    if user is None:
        if args.stdin:
            user = current.admin_user
        else:
            typed = input(f"Username [{current.admin_user}]: ").strip()
            user = typed or current.admin_user

    if args.stdin:
        password = confirm = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("New password: ")
        confirm = getpass.getpass("Repeat it: ")

    try:
        user, password_hash = admin_credentials(
            user, password, confirm, args.algo,
            too_long_hint="Run it again with --algo argon2 to use this passphrase as it is.",
        )
    except SetupInputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        store.save(
            ManagerState(
                admin_user=user,
                admin_password_hash=password_hash,
                session_secret=(
                    current.session_secret if args.keep_sessions else new_session_secret()
                ),
                setup_completed=True,
            )
        )
    except StateStoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Saved. The admin account is now {user!r}.")
    if not args.keep_sessions:
        print("Everyone who was signed in will have to sign in again.")
    print("Restart the manager to use it:  docker compose restart manager")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
