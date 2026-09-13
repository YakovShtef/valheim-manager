"""The manager's own credentials, owned by the manager rather than the operator.

Before this existed, installing meant hand-writing ``manager.env``: a bcrypt hash
generated with a CLI tool and an invented ``SESSION_SECRET``, both of which Compose
was eager to mangle (see ``auth.COMPOSE_TRAP``). The first-run wizard writes them
here instead, on a volume only the manager touches, so there is nothing left for
Compose to interpolate.

The document, at mode ``0600``::

    {
      "version": 1,
      "admin_user": "odin",
      "admin_password_hash": "$2b$12$...",
      "session_secret": "...",
      "setup_completed": true,
      "created_at": "2026-09-12T18:04:11Z"
    }

Nothing in here is ever logged. A file that exists but cannot be read as that
document is a hard error: silently falling back to "unconfigured" would reopen the
setup wizard, and whoever reached the port first would own the manager.

``ADMIN_USER`` / ``ADMIN_PASSWORD_HASH`` / ``SESSION_SECRET`` in the environment
still win and skip this file entirely -- see ``main._resolve_credentials``.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

STATE_VERSION = 1
STATE_MODE = 0o600

# Long enough that the signed cookie is not the weak link; `auth.SessionAuth`
# separately refuses anything under 16 characters.
SESSION_SECRET_BYTES = 48


class StateStoreError(RuntimeError):
    """The state file is unusable, so the manager must refuse to start."""


@dataclass(frozen=True)
class ManagerState:
    """Everything the manager needs to authenticate its single admin.

    ``setup_completed`` is recorded for the operator's benefit; what actually closes
    the wizard is the presence of usable credentials, here or in the environment.
    Trusting the flag instead would mean a file edited to ``false`` reopens admin
    creation on a manager that already has an admin.
    """

    admin_user: str
    admin_password_hash: str
    session_secret: str
    setup_completed: bool = True


def new_session_secret() -> str:
    """A fresh cookie-signing secret. Generated once, then persisted forever."""
    return secrets.token_urlsafe(SESSION_SECRET_BYTES)


def _uid_note() -> str:
    """Which uid is complaining -- the usual cause is a root-owned mount."""
    geteuid = getattr(os, "geteuid", None)
    return f" (running as uid {geteuid()})" if geteuid is not None else ""


def fsync_directory(directory: Path) -> None:
    """Persist a rename, not just the bytes it renamed.

    ``os.replace`` is atomic against a *process* crash, but the directory entry it
    rewrote can still sit in the host's page cache when the machine loses power --
    and losing the rename of this file means losing the admin account, which the
    module docstring above says must never happen. Best effort: opening a directory
    is not possible on Windows and fsync on one is a no-op on some filesystems, and
    neither is worth failing a write over.

    Shared with ``settings_store``, which makes the same atomicity promise about the
    file the game server reads.
    """
    try:
        fd = os.open(str(directory), getattr(os, "O_DIRECTORY", os.O_RDONLY))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class StateStore:
    """Loads and atomically saves the manager's credential state file."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    # ------------------------------------------------------------------ read

    # Deliberately no `exists()`: every caller must go through `load()`, which either
    # returns a complete document or raises. A bare existence check invites the one
    # mistake that matters here -- treating an unreadable file as "not set up yet"
    # and reopening the wizard on a manager that already has an admin.

    def load(self) -> ManagerState | None:
        """The saved credentials, or ``None`` when setup has never run.

        Raises ``StateStoreError`` for a file that exists but is not a complete
        state document -- never ``None``, which would reopen the setup wizard.
        """
        # Checked up front rather than relying on IsADirectoryError: Windows raises
        # PermissionError for the same situation, and the operator deserves the same
        # message either way.
        if self.path.is_dir():
            raise StateStoreError(
                f"The manager state path {self.path} is a directory, not a file. "
                "Remove it (or point MANAGER_STATE_FILE elsewhere) and start again."
            )
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except IsADirectoryError as exc:  # pragma: no cover - covered by is_dir above
            raise StateStoreError(
                f"The manager state path {self.path} is a directory, not a file."
            ) from exc
        except OSError as exc:
            raise StateStoreError(
                f"Could not read the manager state file {self.path}: {exc}{_uid_note()}."
            ) from exc

        try:
            document = json.loads(text)
        except ValueError as exc:
            raise StateStoreError(
                f"The manager state file {self.path} is not valid JSON: {exc}. It holds "
                "the admin account, so the manager will not guess -- restore it from a "
                "backup, or delete it to run first-run setup again (which creates a new "
                "admin account and signs out every existing session)."
            ) from exc
        if not isinstance(document, dict):
            raise StateStoreError(
                f"The manager state file {self.path} does not contain a JSON object."
            )

        version = document.get("version", STATE_VERSION)
        if not isinstance(version, int) or isinstance(version, bool) or version > STATE_VERSION:
            raise StateStoreError(
                f"The manager state file {self.path} declares version {version!r}, which "
                f"this manager does not understand (it writes version {STATE_VERSION}). "
                "This is a newer manager's file -- upgrade the image rather than letting "
                "an older one guess at it."
            )

        missing = [
            key
            for key in ("admin_user", "admin_password_hash", "session_secret")
            if not isinstance(document.get(key), str) or not document.get(key, "").strip()
        ]
        if missing:
            raise StateStoreError(
                f"The manager state file {self.path} is incomplete: "
                f"{', '.join(missing)} missing or empty. Restore it from a backup, or "
                "delete it to run first-run setup again."
            )
        return ManagerState(
            admin_user=document["admin_user"],
            admin_password_hash=document["admin_password_hash"],
            session_secret=document["session_secret"],
            setup_completed=bool(document.get("setup_completed", True)),
        )

    # ----------------------------------------------------------------- write

    def ensure_writable(self) -> None:
        """Fail now, naming the path, rather than at the end of the wizard.

        Called on an unconfigured boot: an operator who has just run
        ``docker compose up -d`` should learn that the state volume is unusable from
        the manager's first log line, not after typing a password into a form.
        """
        parent = self.path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StateStoreError(
                f"The manager state directory {parent} cannot be created: {exc}"
                f"{_uid_note()}. That directory is the `valheim-manager-state` volume "
                "mounted by docker-compose.yml; the manager stores the admin account "
                "there and cannot run first-run setup without it."
            ) from exc
        if not parent.is_dir():
            raise StateStoreError(
                f"The manager state directory {parent} is not a directory."
            )
        probe = parent / f".write-probe-{os.getpid()}"
        try:
            with open(probe, "wb"):
                pass
        except OSError as exc:
            raise StateStoreError(
                f"The manager state directory {parent} is not writable: {exc}"
                f"{_uid_note()}. It is the `valheim-manager-state` volume mounted by "
                "docker-compose.yml; fix its ownership, or set MANAGER_UID/MANAGER_GID "
                "in .env to the uid that owns it, so the manager can store the admin "
                "account there."
            ) from exc
        finally:
            try:
                probe.unlink()
            except OSError:  # pragma: no cover - best effort cleanup
                pass

    def _created_at(self) -> str:
        """The existing document's timestamp, or now for a first write."""
        try:
            existing = json.loads(self.path.read_text(encoding="utf-8"))
            stamp = existing.get("created_at")
            if isinstance(stamp, str) and stamp.strip():
                return stamp
        except (OSError, ValueError, AttributeError):
            pass
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def save(self, state: ManagerState) -> None:
        """Write the state file atomically at mode ``0600``.

        Temp-then-replace in the same directory: a crash mid-write leaves the
        previous file intact rather than a half-written credential store. The mode
        is set on the temp file *before* the rename, so the secrets are never
        readable by anyone else, not even for an instant.
        """
        self.ensure_writable()
        document = {
            "version": STATE_VERSION,
            "admin_user": state.admin_user,
            "admin_password_hash": state.admin_password_hash,
            "session_secret": state.session_secret,
            "setup_completed": bool(state.setup_completed),
            # When the admin account was created, not when this file was last
            # rewritten -- so it stays meaningful if the document is ever re-saved.
            "created_at": self._created_at(),
        }
        parent = self.path.parent
        try:
            fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=".state-", suffix=".tmp")
        except OSError as exc:
            # ensure_writable passed moments ago, so this is the volume filling up or
            # going away underneath us -- name both, or the wizard returns a bare 500.
            raise StateStoreError(
                f"Could not create a temporary file in the manager state directory "
                f"{parent}: {exc}{_uid_note()}. The admin account cannot be saved until "
                "that directory is writable and has free space."
            ) from exc
        try:
            os.chmod(tmp_name, STATE_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except OSError as exc:
            try:
                os.unlink(tmp_name)
            except OSError:  # pragma: no cover - the temp file may already be gone
                pass
            raise StateStoreError(
                f"Could not write the manager state file {self.path}: {exc}{_uid_note()}."
            ) from exc
        # os.replace keeps the temp file's mode, but an existing file replaced on a
        # filesystem that ignores chmod (a Windows bind mount) would not -- so say it
        # again rather than assume.
        try:
            os.chmod(self.path, STATE_MODE)
        except OSError:  # pragma: no cover - filesystem without POSIX modes
            pass
        fsync_directory(parent)


__all__ = [
    "ManagerState",
    "StateStore",
    "StateStoreError",
    "STATE_MODE",
    "fsync_directory",
    "new_session_secret",
]
