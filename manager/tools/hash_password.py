#!/usr/bin/env python3
"""Generate an ADMIN_PASSWORD_HASH for the manager.

NOT needed for a normal install. `docker compose up -d` prints a first-run setup URL
and the wizard there hashes the password for you, straight onto a volume Compose
never reads. This tool remains the escape hatch for the advanced path: pinning
`ADMIN_USER`/`ADMIN_PASSWORD_HASH`/`SESSION_SECRET` in `manager.env` yourself, which
skips first-run setup entirely.

Usage (inside the stack, no extra install needed):

    docker compose run --rm --no-deps manager python tools/hash_password.py

Or on any machine with the manager's dependencies installed:

    python manager/tools/hash_password.py --algo argon2

The password is read from the terminal without echoing. Pass ``--stdin`` to read it
from a pipe instead. Nothing is written to disk -- copy the printed line into
``manager.env`` (bare, unquoted). Never into ``.env``: Compose interpolates ``$``
there and would silently truncate the hash.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

# Allow running as a plain script from the repo without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth import (  # noqa: E402
    BCRYPT_MAX_BYTES,
    PasswordTooLongError,
    hash_password,
    validate_password_hash,
)

MIN_LENGTH = 10


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hash an admin password for the Valheim manager.")
    parser.add_argument(
        "--algo",
        choices=("bcrypt", "argon2"),
        default="bcrypt",
        help=(
            f"bcrypt (default) ignores anything past {BCRYPT_MAX_BYTES} bytes and is "
            "refused above it; argon2 has no length limit"
        ),
    )
    parser.add_argument("--rounds", type=int, default=12, help="bcrypt cost (default 12)")
    parser.add_argument("--stdin", action="store_true", help="read the password from stdin")
    parser.add_argument(
        "--allow-short", action="store_true", help=f"skip the {MIN_LENGTH}-character minimum"
    )
    args = parser.parse_args(argv)

    if args.stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("Admin password: ")
        if password != getpass.getpass("Repeat password: "):
            print("Passwords do not match.", file=sys.stderr)
            return 2

    if not password:
        print("Empty password.", file=sys.stderr)
        return 2
    if len(password) < MIN_LENGTH and not args.allow_short:
        print(
            f"Password is shorter than {MIN_LENGTH} characters. "
            "Use a longer one, or pass --allow-short.",
            file=sys.stderr,
        )
        return 2

    try:
        digest = hash_password(password, algo=args.algo, rounds=args.rounds)
    except PasswordTooLongError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    validate_password_hash(digest)  # sanity check before we hand it to the operator

    print()
    print("Add this line to  manager.env  (NOT .env):")
    print()
    print(f"ADMIN_PASSWORD_HASH={digest}")
    print()
    print("Paste it exactly as printed:")
    print("  * NO QUOTES -- manager.env is loaded with `format: raw`, so any quotes")
    print("    you add become part of the hash.")
    print("  * NOT IN .env -- Compose interpolates `$` there (and in a plain")
    print(f"    env_file), which wrecks this {len(digest)}-character hash:")
    if args.algo == "bcrypt":
        parts = digest.split("$")
        print(f"      it is cut down to `${parts[1]}${parts[2]}`.")
    else:
        # `$argon2id` and `$v` are themselves valid variable names, so they are
        # expanded away and the recognisable prefix disappears completely --
        # e.g. `=19=65536,t=3,p=4...`, not a truncated `$argon2id`.
        print("      the `$argon2id` and `$v=` segments are read as variable names and")
        print("      expanded away, leaving something like `=19=65536,t=3,p=4...`")
        print("      -- mangled past recognition, not merely truncated.")
    print("    Login then fails forever with only \"Invalid username or password\"")
    print("    to show for it.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
