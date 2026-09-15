"""The worlds on the game volume: what is there, what may be uploaded, where it lands.

The Valheim dedicated server loads whichever world ``WORLD_NAME`` names, out of
``/config/worlds_local`` on the ``valheim-config`` volume. It takes no ``-seed``
argument, so importing a world generated locally is the only way an operator ever
gets to choose a seed -- which is what makes uploading worth building at all.

Two layouts are real, and both are accepted:

*1.0*     a **directory** named for the world, holding ``_main.N.db2``,
          ``_main.N.fwl2``, ``_main.N.chunks``, ``_main.N.ok`` and ``*.chunk``.
*legacy*  a ``<name>.db`` + ``<name>.fwl`` pair sitting directly in
          ``worlds_local``. The game converts one to the 1.0 layout the first time
          it loads it, permanently, so an upload of one says so.

Three rules shape everything below.

**Validation precedes placement.** An upload is inspected in full -- shape, entry
names, total size -- before a byte reaches the volume, then written to a hidden
staging directory, and only moved to its real name once it is complete. A half-
written directory that looks like a world is the one thing that could make the
server load a corrupt save, so the rename is the last step.

**Nothing from an archive is trusted.** Entry names are re-rooted and resolved, and
anything that would land outside the destination -- ``../escape``, an absolute path,
a symlink -- is refused before extraction starts, not caught halfway through it.

**Ownership is the trap.** The game server runs as ``PUID:PGID`` (1000) and saves
continuously; the manager runs as uid 10001 with ``cap_drop: [ALL]``, so it cannot
``chown`` what it writes. A world uploaded as ``10001:10001`` would be readable but
not writable, which surfaces later as saves silently not happening -- far worse than
an upload error. The manager therefore runs with the game's *group* and writes
worlds group-writable; see ``WORLD_DIR_MODE`` / ``WORLD_FILE_MODE``.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
import shutil
import stat
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, Sequence

log = logging.getLogger("valheim_manager")

# Where the worlds live, inside the manager container. The game container mounts the
# same volume at the same path; only the manager's mount is new.
DEFAULT_WORLDS_DIR = "/config/worlds_local"

# Where the game server keeps its own hourly backups, and where a manual one goes
# too -- one place to look, rather than a second folder nobody knows about.
DEFAULT_BACKUPS_DIR = "/config/backups"

# The prefix that keeps a manual backup alive. The image prunes its own backups by
# age (BACKUPS_MAX_AGE, 3 days by default), but only files matching its own patterns
# -- `worlds-*.zip` and `AUTOBACKUP-*` -- and it does not recurse. A name outside
# both patterns is never pruned, which is the point: a backup somebody took on
# purpose should not evaporate on a timer.
BACKUP_PREFIX = "MANUAL-"

LAYOUT_MODERN = "1.0"
LAYOUT_LEGACY = "legacy"

# Default cap on one upload, in whole megabytes. A long-played world runs to a few
# hundred MB of chunks, so this leaves room without letting a mistyped drop fill the
# volume. Overridable with WORLD_UPLOAD_MAX_MB.
DEFAULT_MAX_UPLOAD_MB = 1024

# Default cap on the number of files in one upload. A 1.0 world is one file per visited
# map chunk, so a dropped folder is routinely thousands of them -- well past the 1000 a
# multipart parser allows by default. Overridable with WORLD_UPLOAD_MAX_FILES.
DEFAULT_MAX_UPLOAD_FILES = 5000

# Group-writable, because the game server writes these files as PGID and the manager
# cannot chown them afterwards. The setgid bit on the directory makes anything the
# game later creates inside it inherit the same group; it is a best effort, since a
# kernel refuses setgid for a group the process is not in, and the plain mode is what
# actually matters.
WORLD_DIR_MODE = 0o2775
WORLD_DIR_MODE_FALLBACK = 0o775
WORLD_FILE_MODE = 0o664

# A world name is one path segment. Spaces and apostrophes are real -- worlds named
# "Odin's Hall" exist -- so only what makes a name stop being a single segment is
# refused: separators, the dot entries, control characters, and the colon that would
# be a drive letter or an NTFS alternate data stream.
MAX_WORLD_NAME_LENGTH = 64
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# `_main.1.db2` on a live server; the number is a generation counter. The unnumbered
# spelling is accepted too rather than refusing a world over a naming detail.
_MAIN_DB2_RE = re.compile(r"^_main(?:\.\d+)?\.db2$", re.IGNORECASE)
_MAIN_FWL2_RE = re.compile(r"^_main(?:\.\d+)?\.fwl2$", re.IGNORECASE)

# Dropped from an upload before anything is decided: every macOS-made archive carries
# them, and they would otherwise defeat the "do these entries share one root?" test
# and leave the operator with an unexplained refusal.
_JUNK_LEAVES = frozenset({".ds_store", "thumbs.db", "desktop.ini"})
_JUNK_ROOTS = frozenset({"__macosx"})

# Staging lives *inside* worlds_local: the move into place has to be a rename on one
# filesystem to be atomic, and /config itself is often root-owned on the volume. The
# dot prefix keeps it out of every listing, and nothing can load it because it does
# not carry a world's name.
_STAGING_PREFIX = ".upload-"

_COPY_CHUNK = 1024 * 1024


class WorldError(RuntimeError):
    """A world action cannot be carried out. The message is written for the operator."""


class WorldCollisionError(WorldError):
    """Something on the volume is already using the name. Its own type, because the
    check happens twice -- once before the upload is written and once just before the
    rename -- and one condition must not answer two different HTTP statuses depending
    on which of the two caught it."""


class WorldTooLargeError(WorldError):
    """Past the upload cap. Raised by the byte counter as well as by the pre-checks,
    for the same reason ``WorldCollisionError`` exists."""


def human_size(size: int) -> str:
    """Bytes as the operator would say them, for a message about a size limit."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError  # pragma: no cover - the loop always returns


def sanitised_name(raw: str, *, what: str = "world name") -> str:
    """A single path segment, or ``WorldError`` naming what is wrong with it.

    This is the only door: a name reaches the filesystem, ``WORLD_NAME`` or an archive
    destination through here and nowhere else.
    """
    name = (raw or "").strip()
    if not name:
        raise WorldError(f"Give the {what}.")
    if _CONTROL_RE.search(name):
        raise WorldError(f"The {what} cannot contain control characters.")
    if "/" in name or "\\" in name:
        raise WorldError(
            f"The {what} is one save name, not a path, so it cannot contain a "
            "slash or a backslash."
        )
    if ":" in name:
        raise WorldError(f"The {what} cannot contain a colon.")
    if name in (".", ".."):
        raise WorldError(f"{name!r} is not a {what}.")
    if name.startswith("."):
        raise WorldError(
            f"The {what} cannot start with a dot -- that is how the manager hides "
            "its own working folders."
        )
    # Surrounding whitespace was stripped above, so only the dot is left to catch --
    # Windows silently drops a trailing one, which would make `World.` and `World` the
    # same directory and the collision check a lie.
    if name.endswith("."):
        raise WorldError(f"The {what} cannot end with a dot.")
    if len(name) > MAX_WORLD_NAME_LENGTH:
        raise WorldError(
            f"The {what} must be at most {MAX_WORLD_NAME_LENGTH} characters."
        )
    # Belt and braces: after the checks above this cannot differ, and if some future
    # edit lets it, the failure is a refusal rather than a write outside the volume.
    if name != os.path.basename(name):  # pragma: no cover - defensive
        raise WorldError(f"{raw!r} is not a single {what}.")
    return name


# --------------------------------------------------------------- what is there


@dataclass(frozen=True)
class Backup:
    """One manual backup, as the panel reports it."""

    name: str
    size_bytes: int
    world: str

    @property
    def size(self) -> str:
        return human_size(self.size_bytes)


@dataclass(frozen=True)
class World:
    """One world on the volume, as the panel lists it."""

    name: str
    layout: str
    size_bytes: int
    file_count: int
    # Why the switch would refuse this world's name, or "" when it would not. The list
    # and the switch have to agree: offering Load on a row the switch then rejects for
    # a name the operator never typed is the worst of both answers.
    unloadable: str = ""

    @property
    def legacy(self) -> bool:
        return self.layout == LAYOUT_LEGACY

    def as_dict(self, *, active: bool = False) -> dict[str, object]:
        return {
            "name": self.name,
            "layout": self.layout,
            "legacy": self.legacy,
            "size_bytes": self.size_bytes,
            "size": human_size(self.size_bytes),
            "files": self.file_count,
            "active": active,
            "loadable": not self.unloadable,
            "unloadable": self.unloadable,
        }


def unloadable_reason(name: str) -> str:
    """Why ``WORLD_NAME`` could not be pointed at this world, or ``""``.

    The switch runs every name through ``sanitised_name``, so a world whose directory
    was created outside the manager with a name that rule refuses can be listed but
    never loaded -- and saying so on the row is the only way the operator finds out
    without being handed a 400 about a name they never typed.
    """
    try:
        if sanitised_name(name) != name:
            return (
                "Its name has leading or trailing whitespace, which the manager would "
                "have to change to select it."
            )
    except WorldError as exc:
        return str(exc)
    return ""


def _dir_world(entry_path: Path) -> World | None:
    """A directory as a 1.0 world, or ``None`` when it is not one."""
    try:
        children = list(os.scandir(entry_path))
    except OSError:
        # Unreadable subdirectory: not a world we can offer, and not a reason to fail
        # the whole listing.
        return None
    has_db2 = any(_MAIN_DB2_RE.match(child.name) for child in children)
    has_fwl2 = any(_MAIN_FWL2_RE.match(child.name) for child in children)
    if not (has_db2 and has_fwl2):
        return None
    total = 0
    files = 0
    for child in children:
        try:
            if child.is_file(follow_symlinks=False):
                total += child.stat(follow_symlinks=False).st_size
                files += 1
        except OSError:  # pragma: no cover - a file removed mid-scan
            continue
    return World(
        name=entry_path.name,
        layout=LAYOUT_MODERN,
        size_bytes=total,
        file_count=files,
        unloadable=unloadable_reason(entry_path.name),
    )


class WorldStore:
    """The worlds directory on the game volume: listing, collision checks, placement."""

    def __init__(
        self,
        worlds_dir: str | os.PathLike[str] = DEFAULT_WORLDS_DIR,
        *,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_MB * 1024 * 1024,
        max_upload_files: int = DEFAULT_MAX_UPLOAD_FILES,
        backups_dir: str | os.PathLike[str] = DEFAULT_BACKUPS_DIR,
    ):
        self.root = Path(worlds_dir)
        self.backups_dir = Path(backups_dir)
        # The two limits on one upload, kept here so the route, the panel and the
        # placement all read the same number rather than each holding a copy.
        self.max_upload_bytes = max_upload_bytes
        self.max_upload_files = max_upload_files

    # ------------------------------------------------------------------ listing

    def worlds(self) -> list[World]:
        """Every world on the volume, sorted by name. Empty when nothing is there yet.

        A missing directory is *not* an error: on a volume the game server has never
        started against, ``worlds_local`` simply does not exist, and the panel says so
        rather than showing a fault the operator cannot act on.
        """
        if not self.root.exists():
            return []
        if not self.root.is_dir():
            raise WorldError(
                f"{self.root} is not a directory, so the manager cannot read the worlds "
                "on the game volume."
            )
        try:
            entries = list(os.scandir(self.root))
        except OSError as exc:
            raise WorldError(
                f"Could not read the worlds folder ({exc}). Run `docker compose up -d` on the server -- that re-runs the one-off step that grants the manager access to the worlds folder -- then try again."
            ) from exc

        found: list[World] = []
        # Keyed by the LOWERED base name: `World.db` and `world.fwl` are one world on a
        # case-insensitive volume, and pairing them case-sensitively would refuse an
        # upload for a `.fwl` the operator plainly supplied. The entry keeps its own
        # spelling for everything the operator reads.
        legacy_db: dict[str, os.DirEntry] = {}
        legacy_fwl: dict[str, os.DirEntry] = {}
        for entry in entries:
            if entry.name.startswith("."):
                # Includes this module's own staging directories.
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    world = _dir_world(Path(entry.path))
                    if world is not None:
                        found.append(world)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:  # pragma: no cover - removed mid-scan
                continue
            lowered = entry.name.lower()
            if lowered.endswith(".db"):
                legacy_db.setdefault(lowered[: -len(".db")], entry)
            elif lowered.endswith(".fwl"):
                legacy_fwl.setdefault(lowered[: -len(".fwl")], entry)

        claimed = {world.name.lower() for world in found}
        for key in legacy_db.keys() & legacy_fwl.keys():
            if key in claimed:
                # A directory world already owns this name, and the pair beside it is
                # not a second world: listing both would put two rows on screen, mark
                # both active, and leave the collision check seeing only one of them.
                continue
            db_entry = legacy_db[key]
            # The `.db` is the world's save data, so its spelling is the world's name.
            base = db_entry.name[: -len(".db")]
            total = 0
            for entry in (db_entry, legacy_fwl[key]):
                try:
                    total += entry.stat(follow_symlinks=False).st_size
                except OSError:  # pragma: no cover
                    continue
            found.append(
                World(
                    name=base,
                    layout=LAYOUT_LEGACY,
                    size_bytes=total,
                    file_count=2,
                    unloadable=unloadable_reason(base),
                )
            )
        return sorted(found, key=lambda world: world.name.lower())

    def find(self, name: str) -> World | None:
        """The listed world of this name, or ``None``. Case-insensitive.

        Only for reading a world back; it is NOT the collision check -- see
        ``refuse_collision`` for why the list is the wrong thing to ask.
        """
        lowered = name.lower()
        for world in self.worlds():
            if world.name.lower() == lowered:
                return world
        return None

    def blocking_entries(self, name: str) -> list[str]:
        """Entry names already on the volume that a world called ``name`` would land on.

        Asked of the directory itself rather than of ``worlds()``, and that distinction
        is the whole point: the listing deliberately hides a *half* world -- a lone
        ``.db``, or a pair whose halves are spelled differently -- and a name invisible
        to the listing is exactly the one an upload would rename straight over without
        a word. Case-insensitive, because the volume may well be.
        """
        wanted = {candidate.lower() for candidate in _destination_names(name)}
        try:
            entries = list(os.scandir(self.root))
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise WorldError(
                f"Could not read the worlds directory {self.root}: {exc}. Nothing was "
                "written."
            ) from exc
        return sorted(entry.name for entry in entries if entry.name.lower() in wanted)

    def refuse_collision(self, name: str) -> None:
        """Raise unless nothing on the volume stands where ``name`` would be written."""
        blocking = self.blocking_entries(name)
        if blocking:
            raise WorldCollisionError(collision_message(name, blocking))

    def delete(self, name: str) -> list[str]:
        """Remove every entry a world called ``name`` occupies. Returns what went.

        The only destructive operation in this project, so it is deliberately narrow:

        * The name goes through ``sanitised_name`` first, so a path, a traversal or a
          stray colon is refused before anything is looked at, let alone removed.
        * What gets removed is ``blocking_entries`` -- the same case-insensitive read
          of the directory that the upload uses to refuse a collision. That means a
          *half* world (a lone ``.db``, or a pair whose halves are spelled
          differently) is removed too. It is invisible in the listing, it is in the
          way of an upload, and leaving it behind would be the one outcome nobody
          asked for.
        * Every entry is re-resolved against the root before it is touched, and a
          symlink is refused rather than followed. A link in ``worlds_local`` pointing
          at ``/`` is not something to find out about afterwards.

        Whether the server is off, and whether this is the world it is set to load,
        are the caller's business -- the route answers both before it gets here.
        """
        safe = sanitised_name(name)
        entries = self.blocking_entries(safe)
        if not entries:
            raise WorldError(
                f"There is no world called {safe!r} on the server. Nothing was "
                "deleted -- refresh the list."
            )
        removed: list[str] = []
        for entry in entries:
            target = _resolved_within(self.root, entry)
            try:
                if target.is_symlink():
                    raise WorldError(
                        f"{entry!r} is a link, not a world, so the manager will not "
                        "delete it -- a link can point anywhere. Remove it on the "
                        "host if you meant to."
                    )
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            except WorldError:
                raise
            except OSError as exc:
                raise WorldError(
                    f"Could not delete {entry!r} ({exc}). Run `docker compose up -d` on the server -- that re-runs the one-off step that grants the manager access to the worlds folder -- then try again. "
                    + ("Nothing was deleted." if not removed else
                       f"Already removed: {', '.join(removed)}.")
                ) from exc
            removed.append(entry)
        log.info("World %r deleted from %s (%s).", safe, self.root, ", ".join(removed))
        return removed

    def backup(self, name: str) -> "Backup":
        """Zip one world into the backups folder and return what was written.

        Unlike every other world action this does NOT require the server to be off,
        and that is deliberate: it only ever reads the world and writes somewhere
        else, so there is nothing for it to corrupt. A backup taken while people are
        playing is a copy of whatever the server last flushed to disk -- which is
        exactly what the game's own hourly backup is too.

        The zip is built under a dotted temporary name and renamed into place, so a
        half-written archive never carries a name that looks like a backup.
        """
        safe = sanitised_name(name)
        entries = self.blocking_entries(safe)
        if not entries:
            raise WorldError(
                f"There is no world called {safe!r} on the server, so there is "
                "nothing to back up."
            )
        self._ensure_backups_dir()

        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        target = self._free_backup_path(safe, stamp)
        staging = target.with_name("." + target.name + ".part")
        try:
            with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED) as archive:
                for entry in entries:
                    self._add_to_archive(archive, entry)
        except OSError as exc:
            staging.unlink(missing_ok=True)
            raise WorldError(
                f"Could not write the backup ({exc}). Nothing was saved."
            ) from exc
        except Exception:
            staging.unlink(missing_ok=True)
            raise

        _set_mode(staging, WORLD_FILE_MODE)
        _move(staging, target)
        size = target.stat().st_size
        log.info("World %r backed up to %s (%s).", safe, target, human_size(size))
        return Backup(name=target.name, size_bytes=size, world=safe)

    def _ensure_backups_dir(self) -> None:
        if self.backups_dir.is_dir():
            return
        if self.backups_dir.exists():
            raise WorldError(f"{self.backups_dir} exists but is not a directory.")
        try:
            self.backups_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorldError(
                f"Could not open the backups folder ({exc}). Run `docker compose up -d` "
                "on the server -- that re-runs the one-off step that grants the manager "
                "access -- then try again. Nothing was saved."
            ) from exc
        _set_mode(self.backups_dir, WORLD_DIR_MODE, fallback=WORLD_DIR_MODE_FALLBACK)

    def _free_backup_path(self, safe: str, stamp: str) -> Path:
        """``MANUAL-<world>-<stamp>.zip``, with a counter if that second is taken.

        Two backups of one world inside the same second is a double-click, and
        silently overwriting the first would make the button a liar.
        """
        candidate = self.backups_dir / f"{BACKUP_PREFIX}{safe}-{stamp}.zip"
        suffix = 2
        while candidate.exists():
            candidate = self.backups_dir / f"{BACKUP_PREFIX}{safe}-{stamp}-{suffix}.zip"
            suffix += 1
        return candidate

    def _add_to_archive(self, archive: "zipfile.ZipFile", entry: str) -> None:
        """One entry of a world: a 1.0 world's directory, or one pre-1.0 file.

        Entry names are re-resolved against the worlds directory for the same reason
        an upload's are: nothing here should be able to read outside it, and a symlink
        is skipped rather than followed.
        """
        source = _resolved_within(self.root, entry)
        if source.is_symlink():
            return
        if source.is_file():
            archive.write(source, arcname=entry)
            return
        for path in sorted(source.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            archive.write(path, arcname=str(Path(entry) / path.relative_to(source)))

    # ---------------------------------------------------------------- placement

    def _ensure_root(self) -> None:
        """The worlds directory, created here if the volume lets the manager do it.

        Normally the game server has already made it, owned by ``PUID:PGID``. On a
        brand-new volume it does not exist at all, and whether the manager may create
        it comes down to who owns ``/config`` -- which is why the failure names the
        alternative rather than pretending the manager could have done it.
        """
        if self.root.is_dir():
            return
        if self.root.exists():
            raise WorldError(f"{self.root} exists but is not a directory.")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorldError(
                f"There is no worlds folder yet and the manager could not make "
                f"one ({exc}). Run `docker compose up -d` on the server -- that re-runs the one-off step that grants the manager access to the worlds folder -- then try again. Nothing was saved."
            ) from exc
        _set_mode(self.root, WORLD_DIR_MODE, fallback=WORLD_DIR_MODE_FALLBACK)

    def place(self, plan: "UploadPlan") -> World:
        """Write a validated upload to a hidden staging directory, then move it home.

        Raises ``WorldError`` and leaves the volume as it found it -- bar the staging
        directory, which is removed -- for anything that goes wrong on the way. The
        rename is the last statement for a reason: until it runs, nothing on the volume
        carries the world's name, so there is no moment at which the server could load
        a half-written world.
        """
        self._ensure_root()
        try:
            staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=self.root))
        except OSError as exc:
            raise WorldError(
                f"Could not write to the worlds folder ({exc}). Run `docker compose up -d` on the server -- that re-runs the one-off step that grants the manager access to the worlds folder -- then try again. "
                "Nothing was saved."
            ) from exc

        try:
            written = 0
            for dest_name, member in plan.items:
                destination = _resolved_within(staging, dest_name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                written += _write_member(
                    member, destination, self.max_upload_bytes - written, self.max_upload_bytes
                )
                _set_mode(destination, WORLD_FILE_MODE)

            # Re-checked as late as possible: reading a few hundred megabytes takes
            # long enough for a second tab to have uploaded the same name meanwhile.
            self.refuse_collision(plan.name)

            if plan.layout == LAYOUT_MODERN:
                _set_mode(staging, WORLD_DIR_MODE, fallback=WORLD_DIR_MODE_FALLBACK)
                _move(staging, self.root / plan.name)
                staging = None  # moved, not ours to clean up
            else:
                # Two files, so two renames -- the metadata one first. A pair is only a
                # loadable world once the `.db` lands, so an interruption between them
                # leaves something the server ignores rather than a world with no save
                # data in it. If the second fails the first is undone: an orphan `.fwl`
                # is a half-world, which the listing does not show and which the next
                # upload of that name would then be standing on.
                staged_fwl = staging / f"{plan.base}.fwl"
                placed_fwl = self.root / f"{plan.name}.fwl"
                _move(staged_fwl, placed_fwl)
                try:
                    _move(staging / f"{plan.base}.db", self.root / f"{plan.name}.db")
                except Exception:
                    try:
                        os.replace(placed_fwl, staged_fwl)
                    except OSError as exc:  # pragma: no cover - best effort
                        log.warning(
                            "Could not undo the partial placement of %s: %s",
                            placed_fwl,
                            exc,
                        )
                    raise
        except Exception:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
            raise
        else:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)

        placed = self.find(plan.name)
        if placed is None:  # pragma: no cover - defensive, the move just succeeded
            raise WorldError(
                f"{plan.name} was written but cannot be read back from {self.root}."
            )
        log.info("World %r (%s) uploaded to %s.", plan.name, plan.layout, self.root)
        return placed


def _destination_names(name: str) -> tuple[str, ...]:
    """Every entry in ``worlds_local`` a world called ``name`` can occupy.

    A 1.0 world is the directory; a pre-1.0 one is the two files beside it. All three
    are the same world to Valheim, so all three are in the way of each other.
    """
    return (name, f"{name}.db", f"{name}.fwl")


def collision_message(name: str, blocking: Sequence[str]) -> str:
    """Never overwrite a world silently: the refusal names what is standing there."""
    return (
        f"{name!r} is already taken on the volume by {', '.join(blocking)}, and the "
        "manager never overwrites a world -- not even a partial one. Upload it under a "
        "different name, or remove what is there on the host first. Nothing was saved."
    )


def _move(source: Path, destination: Path) -> None:
    try:
        os.replace(source, destination)
    except OSError as exc:
        raise WorldError(
            f"Could not move the uploaded world into place at {destination}: {exc}."
        ) from exc


def _set_mode(path: Path, mode: int, *, fallback: int | None = None) -> None:
    """``chmod``, tolerating the platforms and kernels that refuse part of it.

    The manager owns what it just wrote, so the plain modes always apply; only the
    setgid bit can be refused (for a group the process is not a member of), and that
    is a bonus rather than the promise this makes.
    """
    try:
        os.chmod(path, mode)
        return
    except OSError as exc:
        if fallback is None:
            log.warning("Could not set mode %o on %s: %s", mode, path, exc)
            return
    try:
        os.chmod(path, fallback)
    except OSError as exc:  # pragma: no cover - best effort
        log.warning("Could not set mode %o on %s: %s", fallback, path, exc)


def _resolved_within(base: Path, relative: str) -> Path:
    """``base / relative``, proven to stay inside ``base``.

    The planner has already re-rooted every entry to a bare leaf name, so this can
    only fire if that ever stops being true -- which is exactly when a traversal entry
    must still be refused rather than written.
    """
    candidate = base / relative
    try:
        root = base.resolve()
        resolved = candidate.resolve()
    except OSError as exc:  # pragma: no cover - defensive
        raise WorldError(f"Could not resolve the upload entry {relative!r}: {exc}.") from exc
    if resolved != root and root not in resolved.parents:
        raise WorldError(
            f"The upload contains an entry that would be written outside the world "
            f"directory ({relative!r}). Nothing was saved."
        )
    return candidate


def _write_member(member: "Member", destination: Path, remaining: int, cap: int) -> int:
    """Copy one member out, counting bytes against what the cap still allows."""
    written = 0
    try:
        fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            WORLD_FILE_MODE,
        )
    except OSError as exc:
        raise WorldError(f"Could not write {destination.name}: {exc}.") from exc
    try:
        with member.open() as source, os.fdopen(fd, "wb") as out:
            while True:
                chunk = source.read(_COPY_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > remaining:
                    # A zip that declared a small size in its directory and then
                    # produced more. The cap is the cap, whatever the source claimed.
                    raise WorldTooLargeError(oversize_message(None, cap))
                out.write(chunk)
    except WorldError:
        raise
    except OSError as exc:
        raise WorldError(f"Could not write {destination.name}: {exc}.") from exc
    return written


def oversize_message(size: int | None, cap: int) -> str:
    """The one sentence every size refusal uses, with the cap always in it.

    ``size is None`` is the count taken while writing rather than one read off a header
    or an archive's directory -- so it says what happened (the files held more than
    they reported) without claiming anything about unpacking, which a plain folder drop
    does not do.
    """
    measured = (
        f"That upload is {human_size(size)}, which is"
        if size is not None
        else "That upload held more than it said it would, which puts it"
    )
    return (
        f"{measured} over the {human_size(cap)} limit for one world. Nothing was "
        "saved. You can raise the limit with WORLD_UPLOAD_MAX_MB."
    )


# ------------------------------------------------------- what may be uploaded


@dataclass(frozen=True)
class Member:
    """One file an upload carries, named relative to the upload's own root."""

    path: str
    size: int
    _open: Callable[[], BinaryIO]

    @property
    def leaf(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def depth(self) -> int:
        return self.path.count("/")

    def open(self):
        return self._open()


@dataclass(frozen=True)
class UploadPlan:
    """A validated upload: what it is, what it will be called, and what goes where."""

    name: str
    layout: str
    items: tuple[tuple[str, Member], ...]
    base: str = ""

    @property
    def legacy(self) -> bool:
        return self.layout == LAYOUT_LEGACY

    @property
    def total_bytes(self) -> int:
        return sum(member.size for _, member in self.items)


class _Reopen:
    """A form part read as a stream: rewound on entry, never closed by us.

    Closing is the multipart parser's business -- the same temporary file backs the
    ``UploadFile`` the route still holds.
    """

    def __init__(self, handle: BinaryIO):
        self.handle = handle

    def __enter__(self) -> BinaryIO:
        try:
            self.handle.seek(0)
        except (OSError, ValueError) as exc:  # pragma: no cover - defensive
            raise WorldError(f"Could not re-read the uploaded file: {exc}.") from exc
        return self.handle

    def __exit__(self, *_exc) -> bool:
        return False


def _entry_segments(raw: str) -> list[str]:
    """An upload entry's name as safe segments, or ``WorldError``.

    Everything an archive or a browser can put in a name that is not simply "a file
    below this upload" is refused here, before any decision is made about the upload:
    absolute paths, drive letters, UNC paths and every form of ``..``.
    """
    name = (raw or "").replace("\\", "/")
    if not name.strip():
        raise WorldError("The upload contains an entry with no name. Nothing was saved.")
    if _CONTROL_RE.search(name):
        raise WorldError(
            f"The upload contains an entry whose name has control characters in it "
            f"({raw!r}). Nothing was saved."
        )
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise WorldError(
            f"The upload contains an absolute path ({raw!r}), which would be written "
            "outside the world directory. Nothing was saved."
        )
    segments = [part for part in name.split("/") if part not in ("", ".")]
    if any(part == ".." for part in segments):
        raise WorldError(
            f"The upload contains an entry that climbs out of its own directory "
            f"({raw!r}), which would be written outside the world directory. "
            "Nothing was saved."
        )
    if not segments:
        raise WorldError(f"The upload contains an unusable entry name ({raw!r}).")
    return segments


def _is_junk(segments: Sequence[str]) -> bool:
    return segments[0].lower() in _JUNK_ROOTS or segments[-1].lower() in _JUNK_LEAVES


def members_from_parts(parts: Sequence[tuple[str, BinaryIO, int]]) -> list[Member]:
    """Members from a browser's multipart parts, whose names carry the dropped paths."""
    members: list[Member] = []
    for filename, handle, size in parts:
        segments = _entry_segments(filename)
        if _is_junk(segments):
            continue
        members.append(
            Member(path="/".join(segments), size=size, _open=(lambda h=handle: _Reopen(h)))
        )
    return members


def members_from_zip(archive: zipfile.ZipFile) -> list[Member]:
    """Members from a ``.zip``, with every entry name and mode checked first.

    Sizes come from the archive's own directory, which is a claim, not a fact -- it is
    used to refuse an oversized upload cheaply, and ``_write_member`` counts the bytes
    that actually arrive as well.
    """
    members: list[Member] = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        segments = _entry_segments(info.filename)
        # Only the file-*type* bits, and only when the archive actually carries them:
        # a zip written on Windows (or by `writestr`) stores permissions with no type
        # at all, so testing S_ISREG directly would refuse every such archive.
        file_type = (info.external_attr >> 16) & 0o170000
        if file_type == stat.S_IFLNK:
            raise WorldError(
                f"The archive contains a symbolic link ({info.filename!r}). A world is "
                "files, not links, and a link can point anywhere. Nothing was saved."
            )
        if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise WorldError(
                f"The archive contains an entry that is not a regular file "
                f"({info.filename!r}). Nothing was saved."
            )
        if _is_junk(segments):
            continue
        members.append(
            Member(
                path="/".join(segments),
                size=info.file_size,
                _open=(lambda i=info: _ZipEntry(archive, i)),
            )
        )
    return members


class _ZipEntry:
    def __init__(self, archive: zipfile.ZipFile, info: zipfile.ZipInfo):
        self.archive = archive
        self.info = info
        self.handle: BinaryIO | None = None

    def __enter__(self) -> BinaryIO:
        try:
            self.handle = self.archive.open(self.info)
        except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
            raise WorldError(
                f"The archive entry {self.info.filename!r} could not be read: {exc}. "
                "Nothing was saved."
            ) from exc
        return self.handle

    def __exit__(self, *_exc) -> bool:
        if self.handle is not None:
            self.handle.close()
        return False


@contextmanager
def opened_upload(
    parts: Sequence[tuple[str, BinaryIO, int]]
) -> Iterator[tuple[list[Member], str]]:
    """``(members, suggested name)`` for an upload, unpacking an archive if it is one.

    A context manager because a ``.zip``'s members are only readable while the archive
    is open, and the plan built from them is read inside this block.
    """
    if not parts:
        raise WorldError("No files were uploaded.")
    zips = [name for name, _, _ in parts if name.lower().endswith(".zip")]
    if zips and len(parts) > 1:
        raise WorldError(
            "Upload one world at a time: this drop mixes an archive with other files."
        )
    if zips:
        filename, handle, _size = parts[0]
        try:
            handle.seek(0)
        except (OSError, ValueError) as exc:  # pragma: no cover - defensive
            raise WorldError(f"Could not read the uploaded archive: {exc}.") from exc
        try:
            archive = zipfile.ZipFile(handle)
        except (zipfile.BadZipFile, OSError) as exc:
            raise WorldError(
                f"{filename} is not a readable zip archive ({exc}). Nothing was saved."
            ) from exc
        with archive:
            # The leaf, not the path: a browser part named `Backups/Imported.zip`
            # would otherwise suggest `Backups/Imported`, which is refused as a name
            # the operator never typed.
            leaf = filename.replace("\\", "/").rsplit("/", 1)[-1]
            yield members_from_zip(archive), leaf[: -len(".zip")]
        return
    yield members_from_parts(parts), ""


def plan_upload(
    members: Sequence[Member], *, requested_name: str = "", suggested_name: str = ""
) -> UploadPlan:
    """Decide what an upload *is*, and refuse it here if it is not a world.

    Nothing has been written when this raises, and nothing is written by it. The
    ordering matters: shape first, then the name, so an upload that is not a world at
    all is never refused for its name instead.
    """
    members = [member for member in members if member.path]
    if not members:
        raise WorldError("There were no files in that upload.")

    members, stripped = _strip_common_root(members)
    nested = sorted({member.path for member in members if member.depth})
    if nested:
        raise WorldError(
            "That upload holds more than one world (entries in "
            f"{len(set(path.split('/')[0] for path in nested))} sub-directories, for "
            f"instance {nested[0]!r}). Upload one world at a time -- drop the world's "
            "own folder, not the folder that contains it."
        )

    layout, base = _classify(members)
    if layout == LAYOUT_MODERN:
        fallback = stripped or suggested_name
        items = tuple((member.leaf, member) for member in members)
    else:
        fallback = base
        # Exactly the pair, and nothing else the drop happened to carry: a pre-1.0
        # world *is* those two files, and `X.db.old` is Valheim's own backup, not part
        # of the world.
        # Matched case-insensitively, like the pairing in `_classify` that chose the
        # base: `World.db` next to `world.fwl` is one world, not a missing half.
        pair: dict[str, Member] = {}
        for member in members:
            stem, dot, extension = member.leaf.rpartition(".")
            if dot and stem.lower() == base.lower() and extension.lower() in ("db", "fwl"):
                pair.setdefault(extension.lower(), member)
        items = ((f"{base}.db", pair["db"]), (f"{base}.fwl", pair["fwl"]))

    chosen = requested_name.strip() if requested_name else ""
    if not chosen:
        chosen = fallback
    if not chosen:
        raise WorldError(
            "That upload does not say what the world should be called -- a 1.0 world's "
            "name is its folder's name, and this drop had no folder. Type a name and "
            "upload it again."
        )
    name = sanitised_name(chosen)
    return UploadPlan(name=name, layout=layout, items=items, base=base)


def _strip_common_root(members: list[Member]) -> tuple[list[Member], str]:
    """Peel off wrapper directories, returning the innermost one that was peeled.

    A dropped folder arrives as ``MyWorld/_main.1.db2``; an archive of the whole
    ``worlds_local`` holding one world arrives as ``worlds_local/MyWorld/…``. Both
    reduce to bare file names, and the last directory peeled is the world's own name.
    """
    stripped = ""
    while len(members) and all(member.depth for member in members):
        roots = {member.path.split("/", 1)[0] for member in members}
        if len(roots) != 1:
            break
        stripped = roots.pop()
        members = [
            Member(path=member.path.split("/", 1)[1], size=member.size, _open=member._open)
            for member in members
        ]
    return members, stripped


def _classify(members: Sequence[Member]) -> tuple[str, str]:
    """``(layout, legacy base name)``, or ``WorldError`` naming what was missing."""
    leaves = [member.leaf for member in members]
    db2 = [leaf for leaf in leaves if _MAIN_DB2_RE.match(leaf)]
    fwl2 = [leaf for leaf in leaves if _MAIN_FWL2_RE.match(leaf)]
    if db2 or fwl2:
        if not db2:
            raise WorldError(
                "That looks like a 1.0 world but there is no `_main.N.db2` in it -- "
                "that file is the world itself. Nothing was saved."
            )
        if not fwl2:
            raise WorldError(
                "That looks like a 1.0 world but there is no `_main.N.fwl2` in it -- "
                "that file holds the world's name and seed. Nothing was saved."
            )
        return LAYOUT_MODERN, ""

    # Keyed by the lowered base, valued by the spelling the operator actually used:
    # `World.db` beside `world.fwl` is one world, and pairing it case-sensitively
    # would refuse the upload claiming the `.fwl` they did supply is missing.
    db: dict[str, str] = {}
    fwl: dict[str, str] = {}
    for leaf in leaves:
        lowered = leaf.lower()
        if lowered.endswith(".db"):
            db.setdefault(lowered[: -len(".db")], leaf[: -len(".db")])
        elif lowered.endswith(".fwl"):
            fwl.setdefault(lowered[: -len(".fwl")], leaf[: -len(".fwl")])
    shared = db.keys() & fwl.keys()
    if db or fwl:
        if not shared:
            missing = ".fwl" if db else ".db"
            present = ".db" if db else ".fwl"
            have = sorted(db.values() or fwl.values())[0]
            raise WorldError(
                f"A pre-1.0 world is a matching pair: {have}.db and {have}.fwl. This "
                f"upload has only the {present} file, so the {missing} one is missing. "
                "Upload both together. Nothing was saved."
            )
        if len(shared) > 1:
            raise WorldError(
                "That upload holds more than one pre-1.0 world "
                f"({', '.join(sorted(db[key] for key in shared))}). Upload one at a "
                "time. Nothing was saved."
            )
        # The `.db` holds the save data, so its spelling names the world.
        return LAYOUT_LEGACY, db[shared.pop()]

    raise WorldError(
        "That is not a Valheim world. A 1.0 world is a folder holding `_main.N.db2` "
        "and `_main.N.fwl2` (drop the folder, or a .zip of it); a pre-1.0 world is a "
        "matching `.db` and `.fwl` pair. Nothing was saved."
    )


LEGACY_WARNING = (
    "This is a pre-1.0 world (a .db / .fwl pair). The first time the server loads it, "
    "Valheim converts it to the 1.0 folder layout -- permanently, and there is no way "
    "back. Keep your own copy of the pair if you may want the old format again."
)


__all__ = [
    "DEFAULT_MAX_UPLOAD_FILES",
    "DEFAULT_MAX_UPLOAD_MB",
    "DEFAULT_WORLDS_DIR",
    "LAYOUT_LEGACY",
    "LAYOUT_MODERN",
    "LEGACY_WARNING",
    "MAX_WORLD_NAME_LENGTH",
    "WORLD_DIR_MODE",
    "WORLD_FILE_MODE",
    "Member",
    "UploadPlan",
    "World",
    "WorldCollisionError",
    "WorldError",
    "WorldStore",
    "WorldTooLargeError",
    "collision_message",
    "human_size",
    "oversize_message",
    "unloadable_reason",
    "members_from_parts",
    "members_from_zip",
    "opened_upload",
    "plan_upload",
    "sanitised_name",
]
