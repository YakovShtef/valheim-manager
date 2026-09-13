"""Single-admin session auth for the manager.

Credentials: ``ADMIN_USER`` plus ``ADMIN_PASSWORD_HASH``, which must be a bcrypt
(``$2a$``/``$2b$``/``$2y$``) or argon2 (``$argon2...``) hash. A bare SHA digest is
rejected outright rather than silently accepted.

Normally these three values come from the state file the first-run wizard wrote (see
``state_store``), and no operator ever types them. They can still be supplied by
environment instead, in which case they belong in ``manager.env``, which
``docker-compose.yml`` loads with ``format: raw`` -- and NOT in ``.env``: Compose
interpolates ``$`` there (and in a plain ``env_file:``), which silently truncates
every bcrypt and argon2 hash. ``validate_password_hash`` detects that wreckage and
says so; the messages below name ``manager.env`` because that is the only path on
which an operator can have introduced it by hand.

Session: a ``SameSite=Strict``, ``HttpOnly`` cookie signed with ``SESSION_SECRET``.
The secret is persisted rather than regenerated per boot -- by the wizard, or by the
operator -- so restarting the manager does not log the operator out.

No rate limiting or lockout: accepted trade-off for a single-operator LAN tool.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
from dataclasses import dataclass

import bcrypt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE_NAME = "valheim_manager_session"
SESSION_SALT = "valheim-manager-session-v1"

# Generic on purpose -- no user enumeration.
LOGIN_ERROR = "Invalid username or password."

# Structure, not just prefix. A prefix-only check happily accepts `$2b$12` -- which
# is exactly what Docker Compose leaves behind when a hash is put in `.env` and
# interpolated (see COMPOSE_TRAP below) -- and the failure then surfaces only as
# "Invalid username or password", forever, with nothing in the log.
_BCRYPT_FULL_RE = re.compile(r"^\$2[abxy]\$\d{2}\$[./A-Za-z0-9]{53}$")
_BCRYPT_PREFIX_RE = re.compile(r"^\$2[abxy]\$")
_ARGON2_FULL_RE = re.compile(
    r"^\$argon2(?:id|i|d)\$v=\d+"
    r"\$m=\d+,t=\d+,p=\d+(?:,[a-z]+=[^$]*)*"
    r"\$[A-Za-z0-9+/]+=*\$[A-Za-z0-9+/]+=*$"
)
_ARGON2_PREFIX_RE = re.compile(r"^\$argon2")
# What a Compose-mangled argon2 hash looks like: `$argon2id` and `$v=19` are valid
# variable references, so they are replaced with blank strings and the recognisable
# prefix disappears entirely, leaving the parameter segment stranded.
_ARGON2_MANGLED_RE = re.compile(r"m=\d+,t=\d+,p=\d+")
_BARE_DIGEST_RE = re.compile(r"^[0-9a-fA-F]{32,128}$")
_QUOTED_RE = re.compile(r"^(['\"]).*\1$", re.DOTALL)

COMPOSE_TRAP = (
    "This looks like a bcrypt/argon2 hash that Docker Compose truncated. Compose "
    "interpolates `$` in both `.env` and a plain `env_file:`, and every bcrypt "
    "(`$2b$12$...`) and argon2 (`$argon2id$v=19$...`) hash contains `$`, so "
    "everything after the second `$` is silently eaten. ADMIN_PASSWORD_HASH belongs "
    "in `manager.env`, which docker-compose.yml loads with `format: raw` -- put it "
    "there bare, with NO surrounding quotes, and never in `.env`."
)

BCRYPT_MAX_BYTES = 72

_argon2 = PasswordHasher()


class AuthConfigError(RuntimeError):
    """The auth configuration is unusable; the app must refuse to start."""


@dataclass(frozen=True)
class Session:
    user: str


def validate_password_hash(password_hash: str) -> str:
    """Return the hash's algorithm name, or raise ``AuthConfigError``.

    Validates the hash's full structure. Accepting a prefix alone would let a
    Compose-truncated `$2b$12` through, and the operator would then only ever see
    "Invalid username or password" with nothing explaining why.
    """
    value = (password_hash or "").strip()
    if not value:
        raise AuthConfigError(
            "ADMIN_PASSWORD_HASH is not set. Generate one with "
            "`docker compose run --rm --no-deps manager python tools/hash_password.py` "
            "and put it in `manager.env`."
        )

    if _QUOTED_RE.match(value) and len(value) > 1:
        raise AuthConfigError(
            "ADMIN_PASSWORD_HASH is wrapped in quotes. `manager.env` is loaded with "
            "`format: raw`, which takes the value literally -- quotes and all. Remove "
            "the surrounding quotes and leave the hash bare."
        )

    if _BCRYPT_FULL_RE.match(value):
        return "bcrypt"
    if _ARGON2_FULL_RE.match(value):
        return "argon2"

    if _BCRYPT_PREFIX_RE.match(value):
        raise AuthConfigError(
            f"ADMIN_PASSWORD_HASH starts like a bcrypt hash but is not a complete one "
            f"(got {len(value)} characters; a bcrypt hash is 60, ending in a 53-character "
            f"salt+digest tail). {COMPOSE_TRAP}"
        )
    if _ARGON2_PREFIX_RE.match(value) or _ARGON2_MANGLED_RE.search(value):
        raise AuthConfigError(
            "ADMIN_PASSWORD_HASH looks like an argon2 hash with segments missing; a "
            "complete one is `$argon2id$v=<n>$m=<n>,t=<n>,p=<n>$<salt>$<digest>`. "
            f"{COMPOSE_TRAP}"
        )
    if _BARE_DIGEST_RE.match(value):
        raise AuthConfigError(
            "ADMIN_PASSWORD_HASH looks like a bare SHA/MD5 digest. Only bcrypt or "
            "argon2 hashes are accepted -- generate one with "
            "`python tools/hash_password.py`."
        )
    raise AuthConfigError(
        "ADMIN_PASSWORD_HASH is not a recognised bcrypt or argon2 hash. "
        f"{COMPOSE_TRAP}"
    )


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time-ish password check against a bcrypt or argon2 hash."""
    try:
        algo = validate_password_hash(password_hash)
    except AuthConfigError:
        return False
    if algo == "argon2":
        try:
            return _argon2.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


class PasswordTooLongError(ValueError):
    """The password exceeds what the chosen algorithm can actually hash."""


def hash_password(password: str, algo: str = "bcrypt", rounds: int = 12) -> str:
    """Used by ``tools/hash_password.py`` and by the tests.

    Refuses a bcrypt password over 72 bytes rather than hashing a silently
    truncated one. Never switches algorithm on the caller's behalf -- the caller
    decides whether to shorten the passphrase or ask for argon2.
    """
    if algo == "argon2":
        # argon2 imposes no practical length limit, so nothing to guard here.
        return PasswordHasher().hash(password)
    if algo != "bcrypt":
        raise ValueError(f"Unsupported algorithm: {algo!r} (use bcrypt or argon2)")
    encoded = password.encode("utf-8")
    if len(encoded) > BCRYPT_MAX_BYTES:
        raise PasswordTooLongError(
            f"bcrypt ignores everything past {BCRYPT_MAX_BYTES} bytes, and this "
            f"password is {len(encoded)} bytes "
            f"({len(password)} characters), so its tail would be silently dropped "
            "and a shorter password sharing the first "
            f"{BCRYPT_MAX_BYTES} bytes would also unlock the manager. Use a shorter "
            "passphrase, or --algo argon2, which has no such limit."
        )
    return bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=rounds)).decode("ascii")


class SessionAuth:
    """Issues and validates the signed session cookie."""

    def __init__(
        self,
        *,
        admin_user: str,
        admin_password_hash: str,
        session_secret: str,
        max_age_seconds: int = 7 * 24 * 3600,
        cookie_secure: bool = False,
        cookie_name: str = COOKIE_NAME,
    ):
        if not admin_user.strip():
            raise AuthConfigError("ADMIN_USER is not set.")
        if len(session_secret or "") < 16:
            raise AuthConfigError(
                "SESSION_SECRET must be set in `manager.env` to at least 16 characters "
                "and kept stable across restarts (otherwise every restart logs you out). "
                "Generate one with "
                "`python -c \"import secrets;print(secrets.token_urlsafe(48))\"`."
            )
        self.algo = validate_password_hash(admin_password_hash)
        self.admin_user = admin_user.strip()
        self.admin_password_hash = admin_password_hash.strip()
        self.max_age_seconds = max_age_seconds
        self.cookie_secure = cookie_secure
        self.cookie_name = cookie_name
        self._serializer = URLSafeTimedSerializer(session_secret, salt=SESSION_SALT)

    # ------------------------------------------------------------ credentials

    def check_credentials(self, username: str, password: str) -> bool:
        user_ok = hmac.compare_digest(
            (username or "").strip().encode("utf-8"), self.admin_user.encode("utf-8")
        )
        # Always run the hash verification so a wrong username costs the same as a
        # wrong password.
        password_ok = verify_password(password or "", self.admin_password_hash)
        return user_ok and password_ok

    # ---------------------------------------------------------------- cookies

    def issue_token(self, username: str) -> str:
        payload = {"u": username, "n": secrets.token_urlsafe(8)}
        return self._serializer.dumps(json.dumps(payload, separators=(",", ":")))

    def read_token(self, token: str | None) -> Session | None:
        if not token:
            return None
        try:
            raw = self._serializer.loads(token, max_age=self.max_age_seconds)
        except (SignatureExpired, BadSignature):
            return None
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return None
        user = payload.get("u")
        if not isinstance(user, str) or not hmac.compare_digest(
            user.encode("utf-8"), self.admin_user.encode("utf-8")
        ):
            return None
        return Session(user=user)

    def cookie_kwargs(self) -> dict[str, object]:
        return {
            "key": self.cookie_name,
            "httponly": True,
            "samesite": "strict",
            "secure": self.cookie_secure,
            "path": "/",
            "max_age": self.max_age_seconds,
        }
