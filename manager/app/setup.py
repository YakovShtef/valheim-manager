"""The first-run setup wizard: its one-time token, and what completing it writes.

The manager boots unconfigured on a clean install and logs a URL carrying a token
generated here. That token is the whole access control on admin creation: an open
wizard would let whoever loads the port first own a root-equivalent service on the
LAN, so the URL has to come out of ``docker compose logs manager`` (the
Jupyter/Portainer precedent -- see the spec's Design Notes).

The token lives only in this process's memory. It is never written to the state
file, so a manager restart mints a new one and the old URL stops working; and it is
void the moment setup completes, because ``is_open()`` is false from then on.

Completion is split in two on purpose:

``prepare()``  validates every field and hashes the password. Pure -- no writes, so
               a rejected form leaves nothing behind and the token stays usable.
``commit()``   writes the settings file, then the credential state file, then closes
               the wizard. Settings first: a failure there (an unwritable settings
               directory) leaves setup open and retryable, whereas saving the
               credentials first would close the wizard over a half-done install.

Nothing here logs a password, a hash, or the session secret.

``validated_settings`` and ``SETTINGS_KEYS`` are shared, not wizard-private: the
dashboard's settings panel writes the same keys to the same file and has to refuse
exactly what the wizard refuses, so both call this one implementation.

``SERVER_ARGS`` (Valheim's world modifiers) is one of those keys, but only the panel
ever submits it: the wizard deliberately does not ask about difficulty. Its validation
lives in ``modifiers`` and is re-raised here as ``SetupInputError``, so a bad modifier
is refused in exactly the shape a bad port is.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from .auth import PasswordTooLongError, hash_password
from .modifiers import ModifierError, canonical, compose, fields_to_modifiers, parse
from .settings_store import SettingsStore
from .state_store import ManagerState, StateStore, new_session_secret

log = logging.getLogger("valheim_manager")

SETUP_PATH = "/setup"

# 32 bytes -> a 43-character URL-safe token. Long enough that guessing it is not a
# strategy, short enough to paste out of a log line.
SETUP_TOKEN_BYTES = 32

# The wizard is the only way in on a clean install, so it sets the floor for the one
# credential that guards a root-equivalent service. There is deliberately no login
# rate limiting (see README), which is exactly why a short password is refused here.
MIN_PASSWORD_LENGTH = 10

# `valheim_server.x86_64` itself refuses to start without a join password of at
# least 5 characters -- whatever SERVER_PUBLIC says, and with no passwordless option.
# So empty is NOT an allowed answer here: accepting one would mean the operator
# follows the README to the letter, presses Start, and gets a container that exits
# immediately with nothing in the UI but "stopped".
MIN_SERVER_PASS_LENGTH = 5

MAX_ADMIN_USER_LENGTH = 64

# Exactly the keys the wizard writes and the settings panel may edit. Every other key
# in the settings file -- and every comment in it -- is left alone by
# SettingsStore.write. `SERVER_ARGS` is the one key here the wizard never submits: the
# panel composes it from the world-modifier controls, and asking about difficulty
# during a first install is not what the wizard is for.
SETTINGS_KEYS = (
    "SERVER_NAME",
    "WORLD_NAME",
    "SERVER_PORT",
    "SERVER_PASS",
    "SERVER_PUBLIC",
    "CROSSPLAY",
    "SERVER_ARGS",
)

HASH_ALGORITHMS = ("bcrypt", "argon2")


class SetupInputError(ValueError):
    """A submitted field is unusable. The message is safe to show the operator.

    Raised by the wizard and, through ``validated_settings``, by the dashboard's
    settings panel: both run the same checks so the two cannot drift apart.
    """


def new_setup_token() -> str:
    return secrets.token_urlsafe(SETUP_TOKEN_BYTES)


def one_line(value: str) -> str:
    """Reject the CR/LF that would let one field forge another line in the file."""
    return (value or "").replace("\r", "").replace("\n", "")


@dataclass(frozen=True)
class PreparedSetup:
    """A validated wizard submission, hashed and ready to write."""

    state: ManagerState
    settings: dict[str, str]


class SetupSession:
    """The open first-run wizard: its token, its validation, and its one write."""

    def __init__(
        self,
        state_store: StateStore,
        settings: SettingsStore,
        *,
        token: str | None = None,
    ):
        self.state_store = state_store
        self.settings = settings
        self.token = token or new_setup_token()
        self._completed = False

    # -------------------------------------------------------------- the token

    def is_open(self) -> bool:
        """False once setup has completed -- permanently, for this process."""
        return not self._completed

    def token_ok(self, candidate: str | None) -> bool:
        """Constant-time token check. A closed wizard accepts nothing."""
        if self._completed or not candidate:
            return False
        try:
            return hmac.compare_digest(
                candidate.encode("utf-8"), self.token.encode("utf-8")
            )
        except (AttributeError, UnicodeError):  # pragma: no cover - defensive
            return False

    def setup_url(self, base: str = "") -> str:
        """The URL to paste into a browser, token included."""
        query = urlencode({"token": self.token})
        return f"{base.rstrip('/')}{SETUP_PATH}?{query}"

    # ---------------------------------------------------------- validation

    def prepare(
        self,
        *,
        admin_user: str,
        password: str,
        confirm: str,
        algo: str = "bcrypt",
        settings: dict[str, str] | None = None,
    ) -> PreparedSetup:
        """Validate and hash, without touching the filesystem.

        Raises ``SetupInputError`` with a message written for the operator. The
        password itself never reaches the message.
        """
        user = one_line(admin_user).strip()
        if not user:
            raise SetupInputError("Choose an admin username.")
        if len(user) > MAX_ADMIN_USER_LENGTH:
            raise SetupInputError(
                f"The admin username must be at most {MAX_ADMIN_USER_LENGTH} characters."
            )
        if any(character.isspace() for character in user):
            raise SetupInputError("The admin username cannot contain spaces.")

        if password != confirm:
            raise SetupInputError("The two passwords do not match.")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise SetupInputError(
                f"The admin password must be at least {MIN_PASSWORD_LENGTH} characters. "
                "This login controls your whole server, and nothing slows down "
                "limiting, so a short password is the whole exposure."
            )
        if "\n" in password or "\r" in password:
            raise SetupInputError("The admin password cannot contain a line break.")
        if algo not in HASH_ALGORITHMS:
            raise SetupInputError(f"Unknown password algorithm {algo!r}.")

        try:
            password_hash = hash_password(password, algo=algo)
        except PasswordTooLongError as exc:
            # bcrypt's 72-byte limit, surfaced with its own explanation plus the way
            # out that exists on this page.
            raise SetupInputError(
                f"{exc} Choose argon2 below to use this passphrase as it is."
            ) from exc

        return PreparedSetup(
            state=ManagerState(
                admin_user=user,
                admin_password_hash=password_hash,
                session_secret=new_session_secret(),
                setup_completed=True,
            ),
            settings=validated_settings(settings or {}),
        )

    # -------------------------------------------------------------- the write

    def commit(self, prepared: PreparedSetup) -> ManagerState:
        """Write the settings file, then the credentials, then close the wizard.

        Raises ``SettingsFileError`` or ``StateStoreError`` untouched: both name the
        path and the fix, and leaving setup open means the operator can fix the
        mount and resubmit the same URL.
        """
        # Reachable: `prepare` now runs in a threadpool, so two submissions holding
        # the same token can both clear the route's gate. Nothing awaits between here
        # and the flag, so exactly one of them gets through.
        if self._completed:
            raise SetupInputError(
                "Setup has already been completed by another request. Sign in instead."
            )
        self.settings.write(prepared.settings)
        self.state_store.save(prepared.state)
        self._completed = True
        # No password, hash, or secret here -- just the fact and the account name.
        log.warning(
            "First-run setup completed for admin user %r. The setup wizard is now "
            "closed and its token is void. Credentials are stored at %s.",
            prepared.state.admin_user,
            self.state_store.path,
        )
        return prepared.state


def validated_settings(
    raw: dict[str, str], *, current: dict[str, str] | None = None
) -> dict[str, str]:
    """Game settings, checked before anything is written. One implementation, two
    callers: the first-run wizard (which submits every key) and the dashboard's
    settings panel (which submits only what the operator actually changed).

    Only the keys present in ``raw`` are validated and returned, so the panel cannot
    be blocked from fixing the server name by some *other* value that was already
    wrong in the file. ``current`` supplies the stored values that the cross-field
    checks need when the submission does not carry them -- the panel leaves an
    untouched masked secret out entirely, and renaming a server to contain the stored
    join password would still produce a server that refuses to start.
    """
    values = {
        key: one_line(raw.get(key, "")).strip() for key in SETTINGS_KEYS if key in raw
    }
    stored = current or {}

    name = values.get("SERVER_NAME")
    if name is not None and not name:
        raise SetupInputError("Give the server a name.")

    world = values.get("WORLD_NAME")
    if world is not None:
        if not world:
            raise SetupInputError("Give the world a name.")
        if "/" in world or "\\" in world:
            raise SetupInputError(
                "The world name is a save file name, so it "
                "cannot contain a slash."
            )

    if "SERVER_PORT" in values:
        raw_port = values["SERVER_PORT"] or "2456"
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise SetupInputError(f"The game port must be a number, not {raw_port!r}.") from exc
        # The query port (+1) and the crossplay backend port (+2) sit above it, which
        # is why the range stops short of 65535.
        if not 1 <= port <= 65533:
            raise SetupInputError(
                "The game port must be between 1 and 65533 -- Valheim also uses "
                "the crossplay port (+2) have to fit above it."
            )
        values["SERVER_PORT"] = str(port)

    join_password = values.get("SERVER_PASS")
    if join_password is not None and len(join_password) < MIN_SERVER_PASS_LENGTH:
        raise SetupInputError(
            f"The join password must be at least {MIN_SERVER_PASS_LENGTH} characters. "
            "Valheim will not start without one, so leaving it empty "
            "would mean Start produced a container that exits immediately."
        )

    # A cross-field rule, so it is checked against what the file would end up holding:
    # the panel can submit a new name without the password, or a new password without
    # the name, and either way the pair has to be one the game server will accept.
    if join_password is not None or name is not None:
        effective_name = name if name is not None else stored.get("SERVER_NAME", "")
        effective_pass = (
            join_password if join_password is not None else stored.get("SERVER_PASS", "")
        )
        if effective_name and effective_pass and effective_pass.lower() in effective_name.lower():
            raise SetupInputError(
                "The join password cannot appear inside the server name -- Valheim "
                "server refuses to start in that case."
            )

    if "SERVER_PUBLIC" in values and values["SERVER_PUBLIC"] not in ("0", "1"):
        raise SetupInputError("SERVER_PUBLIC must be 0 or 1.")
    if "CROSSPLAY" in values and values["CROSSPLAY"] not in ("true", "false"):
        raise SetupInputError("CROSSPLAY must be true or false.")

    if "SERVER_ARGS" in values:
        # The last gate before the writer, on the string itself rather than on the
        # fields it came from: it has to be exactly what `compose` produces, so a
        # composed value can never reach the file in an order Valheim would apply
        # wrongly, and nothing can slip past by posting SERVER_ARGS ready-made.
        try:
            values["SERVER_ARGS"] = canonical(values["SERVER_ARGS"])
        except ModifierError as exc:
            raise SetupInputError(str(exc)) from exc
    return values


def validated_modifiers(raw: Mapping[str, Any], *, current: str = "") -> str:
    """Compose posted world-modifier fields into a ``SERVER_ARGS`` value.

    ``current`` is the ``SERVER_ARGS`` the file holds now; whatever in it this feature
    does not manage is carried through untouched, so changing a dropdown cannot cost an
    operator an argument they added by hand.

    Refuses with ``SetupInputError`` -- the panel's own refusal shape -- naming the
    field, for any category, value or toggle outside the documented vocabulary.
    """
    try:
        return compose(fields_to_modifiers(raw, unmanaged=parse(current).unmanaged))
    except ModifierError as exc:
        raise SetupInputError(str(exc)) from exc


__all__ = [
    "MIN_PASSWORD_LENGTH",
    "MIN_SERVER_PASS_LENGTH",
    "PreparedSetup",
    "SETTINGS_KEYS",
    "SETUP_PATH",
    "SetupInputError",
    "SetupSession",
    "new_setup_token",
    "one_line",
    "validated_modifiers",
    "validated_settings",
]
