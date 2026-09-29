"""The three permission files at the root of the /config volume.

    adminlist.txt      -- who may use in-game admin commands
    bannedlist.txt     -- who may not join
    permittedlist.txt  -- a WHITELIST: non-empty means only these may join

The manager is a second writer on files the game server owns, which sets two rules.
Comments are preserved verbatim -- the game ships a header line in each file, and a
rewrite that dropped what it could not parse would eat it. And the file is written
mode 0664: the manager runs as uid 10001 in the game's group (gid 1000), so after the
first manager write the file is owned by 10001:1000 and the game server keeps write
access only through the group bit.

Writes are temp-file-plus-os.replace, the same as ``settings_store``. /config is mode
775 group 1000 on the deployment this was built for, so the manager can create a temp
file there; atomicity matters because the reader is a live game server that must
never see a truncated list.

``parked`` entries -- lines of the form ``// disabled-by-manager <id>`` -- are how the
whitelist is switched off without losing its contents. They are comments to the game,
so it ignores them, and the manager can put them back.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .state_store import fsync_directory

log = logging.getLogger(__name__)

ADMIN = "admin"
BANNED = "banned"
PERMITTED = "permitted"

LIST_FILENAMES = {
    ADMIN: "adminlist.txt",
    BANNED: "bannedlist.txt",
    PERMITTED: "permittedlist.txt",
}

LIST_MODE = 0o664

_PARKED_RE = re.compile(r"^//\s*disabled-by-manager\s+(\S+)\s*$")


class PermissionListError(Exception):
    """Raised with text meant to be shown to the operator as-is."""


@dataclass(frozen=True)
class ListFile:
    kind: str
    ids: tuple[str, ...]
    comments: tuple[str, ...]
    parked: tuple[str, ...]


def parse_list_text(kind: str, text: str) -> ListFile:
    comments: list[str] = []
    ids: list[str] = []
    parked: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parked_match = _PARKED_RE.match(line)
        if parked_match:
            parked.append(parked_match.group(1))
            continue
        if line.startswith("//"):
            comments.append(line)
            continue
        ids.append(line)
    return ListFile(kind=kind, ids=tuple(ids), comments=tuple(comments), parked=tuple(parked))


def render_list_text(entry: ListFile) -> str:
    lines = [*entry.comments]
    lines.extend(f"// disabled-by-manager {pid}" for pid in entry.parked)
    lines.extend(entry.ids)
    return "\n".join(lines) + "\n" if lines else ""


class PermissionLists:
    """Reads and atomically writes the three files."""

    def __init__(self, config_dir: str | os.PathLike[str]):
        self.config_dir = Path(config_dir)

    def path_for(self, kind: str) -> Path:
        try:
            return self.config_dir / LIST_FILENAMES[kind]
        except KeyError:
            raise PermissionListError(f"There is no {kind!r} list.") from None

    def read(self, kind: str) -> ListFile:
        path = self.path_for(kind)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # The game creates these on first start. Before that, an empty list is
            # the honest answer, not an error.
            return ListFile(kind=kind, ids=(), comments=(), parked=())
        except OSError as exc:
            raise PermissionListError(
                f"Could not read {path}: {exc}. It must be readable by the manager's uid."
            ) from exc
        return parse_list_text(kind, text)

    def write(self, kind: str, ids: Sequence[str], *, parked: Sequence[str] = ()) -> ListFile:
        path = self.path_for(kind)
        current = self.read(kind)
        entry = ListFile(
            kind=kind, ids=tuple(ids), comments=current.comments, parked=tuple(parked)
        )
        parent = path.parent
        try:
            fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=".valheim-list-", suffix=".tmp")
        except OSError as exc:
            raise PermissionListError(
                f"Could not write to {parent}: {exc}. The manager's uid needs write "
                "access to the game's config volume."
            ) from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(render_list_text(entry))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, LIST_MODE)
            os.replace(tmp_name, path)
            fsync_directory(parent)
        except OSError as exc:
            try:
                os.unlink(tmp_name)
            except OSError:  # pragma: no cover - already gone
                pass
            raise PermissionListError(f"Could not write {path}: {exc}") from exc
        return entry

    def add(self, kind: str, file_id: str) -> ListFile:
        current = self.read(kind)
        if file_id in current.ids:
            return current
        return self.write(kind, (*current.ids, file_id), parked=current.parked)

    def remove(self, kind: str, file_id: str) -> ListFile:
        current = self.read(kind)
        if file_id not in current.ids:
            return current
        kept = tuple(pid for pid in current.ids if pid != file_id)
        return self.write(kind, kept, parked=current.parked)


# Valheim 1.0 addresses players as [Platform]_[UserID], case-sensitive. For Steam the
# working form is "V_" plus the SteamID64 -- established by community-valheim-tools
# issue #798 and by the in-game F2 panel, NOT by the files, whose header comments
# document no format at all. Treated as probable rather than proven: see the spec.
STEAM_FILE_PREFIX = "V_"

_PLATFORM_PREFIXES = {"steam": STEAM_FILE_PREFIX}
_PREFIXED_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*_\S+$")
_BARE_STEAM_RE = re.compile(r"^\d{17}$")


def to_file_id(platform_id: str, platform: str) -> str | None:
    """A platform id as logged, in the form the list files want, or ``None``.

    ``None`` means "this cannot be converted safely" -- the caller shows the row as
    needing its id rather than writing a guess. An invented prefix would produce a
    line the game accepts and silently ignores, which is the worst failure available:
    the operator sees the id in the file and believes it took effect.
    """
    value = platform_id.strip()
    if not value:
        return None
    if _PREFIXED_RE.match(value):
        return value
    prefix = _PLATFORM_PREFIXES.get(platform)
    if prefix is None:
        return None
    return f"{prefix}{value}"


def normalise_typed_id(text: str) -> tuple[str | None, str | None]:
    """What the operator typed, as a file id -- or a refusal to show them.

    Exactly one of the two is non-``None``. Case is never altered: these ids are
    case-sensitive, so "correcting" one would break it.
    """
    value = text.strip()
    if not value:
        return None, "Enter a player ID."
    if _BARE_STEAM_RE.match(value):
        return None, (
            f"That looks like a bare SteamID64. Valheim wants the platform form -- "
            f"probably {STEAM_FILE_PREFIX}{value}. The exact value is shown in the "
            "in-game F2 panel; paste it from there."
        )
    if not _PREFIXED_RE.match(value):
        return None, (
            "A player ID looks like Platform_UserID, for example "
            f"{STEAM_FILE_PREFIX}76561198012345678. You can copy yours from the "
            "in-game F2 panel."
        )
    return value, None


# The image writes these three files from these three variables at every container
# start, overriding whatever is on the volume. A value here silently discards every
# edit the panel makes, on the next start rather than immediately -- which is the
# hardest kind of failure to connect back to its cause.
OVERWRITING_ENV_VARS = ("ADMINLIST_IDS", "BANNEDLIST_IDS", "PERMITTEDLIST_IDS")


def overwriting_env_vars(settings: dict[str, str]) -> list[str]:
    """Which of the three overriding variables are set to a real value."""
    return [name for name in OVERWRITING_ENV_VARS if str(settings.get(name, "")).strip()]


__all__ = [
    "ADMIN",
    "BANNED",
    "LIST_FILENAMES",
    "LIST_MODE",
    "OVERWRITING_ENV_VARS",
    "PERMITTED",
    "STEAM_FILE_PREFIX",
    "ListFile",
    "PermissionListError",
    "PermissionLists",
    "normalise_typed_id",
    "overwriting_env_vars",
    "parse_list_text",
    "render_list_text",
    "to_file_id",
]
