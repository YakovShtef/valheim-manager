"""The backups folder: what is in it, what made it, and what prunes it.

Three different things write archives into ``/config/backups`` and they are not
interchangeable:

* the **game image**, hourly, as ``worlds-*.zip`` and ``AUTOBACKUP-*``. It prunes
  these by age (``BACKUPS_MAX_AGE``, three days by default) and does not recurse --
  but only as part of taking a new one, so a server that has not run for a while
  keeps them: a live one was seen holding archives two weeks past the limit. One
  archive holds the whole worlds folder, so it is named by time, not by world.
* the **Backup button**, as ``MANUAL-<world>-<stamp>.zip``. Outside the image's
  patterns on purpose, so a backup somebody took deliberately never evaporates on
  anyone's timer -- this one included.
* the **backup timer** in this module, as ``SCHEDULED-<world>-<stamp>.zip``. Also
  outside the image's patterns, which means nothing else will ever delete them and
  retention here is not optional: a daily archive of a large world fills a volume in
  a year, and the thing that breaks when it fills is the game server.

So the rule this module enforces is narrow and worth stating once: **retention only
ever deletes SCHEDULED- archives, and only for the world it just backed up.** A
manual backup and the image's own backups are never removed on anyone's timer; the
operator can delete any of them from the panel, one at a time, after being asked.

Restoring reuses the upload path wholesale -- ``members_from_zip``, ``plan_upload``,
``WorldStore.place`` -- rather than growing a second way to write a world. A restore
is an upload whose bytes happen to come from the backups folder, and it gets the same
name checks, the same traversal and symlink refusals, the same size caps and the same
stage-then-rename write as a world dropped onto the drop zone.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import zipfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .state_store import fsync_directory
from .worlds import (
    BACKUP_PREFIX,
    MAX_WORLD_NAME_LENGTH,
    SCHEDULED_PREFIX,
    World,
    WorldError,
    WorldStore,
    human_size,
    members_from_zip,
    plan_upload,
    sanitised_name,
    world_names_in,
)

log = logging.getLogger("valheim_manager")

# What the manager made, keyed by prefix. Order matters only for readability.
KIND_MANUAL = "manual"
KIND_SCHEDULED = "scheduled"
# Everything else in the folder, which in practice means the game's own hourly
# archives. Listed so the panel can show them -- they are the backups an operator is
# most likely to want after a crash. Never pruned by the manager; deleted only when
# the operator asks.
KIND_GAME = "game"

_PREFIXES = {BACKUP_PREFIX: KIND_MANUAL, SCHEDULED_PREFIX: KIND_SCHEDULED}

# What `WorldStore._free_backup_path` appends to a world's name: `-YYYYmmdd-HHMMSS`,
# and a counter when two backups land inside one second. Anchored to the end so only
# the stamp can be taken off, however the world itself is spelled.
_STAMPED_RE = re.compile(r"^(?P<world>.+)-\d{8}-\d{6}(?:-\d+)?$")

# The longest name this module can be asked about is one it wrote itself: the longer
# prefix, a full-length world, the stamp and a counter. Derived rather than written
# out, so it cannot drift if the world-name cap moves. An archive is not a world and
# does not get the world's cap: doing that refused names the manager had just
# composed, which made those backups impossible to delete or restore.
MAX_ARCHIVE_NAME_LENGTH = (
    len(SCHEDULED_PREFIX) + MAX_WORLD_NAME_LENGTH + len("-YYYYmmdd-HHMMSS") + len("-999")
)

# Retention and interval bounds. The floor on the interval is not arbitrary: below an
# hour the timer writes faster than the game's own hourly backup while shrinking the
# history that `keep` buys to a handful of hours, which is the opposite of the point.
MIN_INTERVAL_HOURS = 1
MAX_INTERVAL_HOURS = 720
DEFAULT_INTERVAL_HOURS = 24
MIN_KEEP = 1
MAX_KEEP = 200
DEFAULT_KEEP = 7

# How the timer decides it is owed a backup. `interval` counts hours from the last
# run; `daily` waits for a time on the clock. Both are kept on the schedule at once,
# so switching between them and back finds the other setting where it was left.
MODE_INTERVAL = "interval"
MODE_DAILY = "daily"
_MODES = frozenset({MODE_INTERVAL, MODE_DAILY})

# Early enough that nobody is playing, late enough that a machine asleep overnight
# still reaches it before morning.
DEFAULT_DAILY_TIME = "03:00"

# `H:MM` as well as `HH:MM`: a browser's time field always sends two digits, but the
# API is reachable without one and `1:30` is what a person types.
_DAILY_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_daily_time(raw: Any) -> str | None:
    """``"1:30"`` -> ``"01:30"``, or ``None`` when it is not a time of day.

    One stored spelling per time, so what the panel shows back is what the timer will
    actually do rather than a second way of writing it.
    """
    if not isinstance(raw, str):
        return None
    match = _DAILY_TIME_RE.match(raw.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def _next_daily(after: float, daily_time: str) -> float:
    """The first ``daily_time`` strictly after ``after``, on the wall clock.

    Calendar arithmetic rather than ``after + 86400``: the point of the mode is that
    it keeps the time somebody chose, and a day is not always 86400 seconds. Adding a
    day to the *date* holds 13:30 at 13:30 across a DST change.
    """
    hour, minute = (int(part) for part in daily_time.split(":"))
    moment = datetime.fromtimestamp(after)
    candidate = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= moment:
        candidate += timedelta(days=1)
    return candidate.timestamp()

# How often the timer wakes to ask whether it is due. Coarse on purpose: the question
# is cheap, the answer is almost always no, and a wake-up this frequent means a
# restart cannot step over a window.
TICK_SECONDS = 60


class BackupError(RuntimeError):
    """Something the operator should be told, in words they can act on."""


@dataclass(frozen=True)
class BackupEntry:
    """One archive in the backups folder, as the panel lists it."""

    name: str
    world: str
    kind: str
    size_bytes: int
    taken_at: float
    # The worlds inside a game archive, read from its file list, for the panel to
    # show. Empty for the manager's own archives, which carry their world in `world`,
    # and for anything that cannot be read. Display only: restore never uses it.
    contains: tuple[str, ...] = ()

    @property
    def size(self) -> str:
        return human_size(self.size_bytes)

    @property
    def restorable(self) -> bool:
        """Whether this module is willing to read it back as a world.

        Only ``.zip``. The game writes some of its own backups as plain ``.db``/
        ``.fwl`` pairs, and reading one of those back is guesswork about which two
        files belong together -- so they are listed, and Restore is not offered.
        """
        return self.name.lower().endswith(".zip")

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "world": self.world,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "size": self.size,
            "taken_at": self.taken_at,
            "restorable": self.restorable,
            "contains": list(self.contains),
            # Every row, the game's own included. The image prunes its archives only
            # when it takes a new one, so on a server that has been off they stay
            # until someone removes them -- and that someone needs a button.
            "deletable": True,
        }


@dataclass(frozen=True)
class BackupSchedule:
    """The backup timer's settings, as stored and as the panel edits them."""

    enabled: bool = True
    # `interval` or `daily`. Both settings below are kept whichever is in force, so
    # switching to the other and back does not lose the one you had.
    mode: str = MODE_INTERVAL
    interval_hours: int = DEFAULT_INTERVAL_HOURS
    daily_time: str = DEFAULT_DAILY_TIME
    keep_per_world: int = DEFAULT_KEEP
    # When the timer last completed a backup, as an epoch. Persisted so restarting
    # the manager neither restarts the clock nor fires a backup on every boot -- with
    # this absent, a container that restarts hourly would back up hourly.
    last_run_at: float | None = None
    # Why the last run did not produce an archive, or "" if it did. Kept so the panel
    # can say "no world loaded yet" rather than showing a timer that looks broken.
    last_error: str = ""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    def due_at(self) -> float | None:
        """When the next run is owed, or None if the timer is off."""
        if not self.enabled:
            return None
        if self.last_run_at is None:
            # Never run: due now. A fresh install gets one backup immediately rather
            # than nothing at all for the first interval.
            return 0.0
        if self.mode == MODE_DAILY:
            # A due time already past -- the manager was down at 13:30, or the world
            # was not loaded -- stays in the past, so the next tick takes it late
            # rather than skipping the day. `last_run_at` then moves, so a machine
            # that restarts five times in an hour still gets one backup.
            return _next_daily(self.last_run_at, self.daily_time)
        return self.last_run_at + self.interval_hours * 3600


def _clamp(value: Any, low: int, high: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def schedule_from_payload(payload: dict[str, Any], current: BackupSchedule) -> BackupSchedule:
    """A schedule built from what the panel posted, clamped to what is sane.

    Clamped rather than refused: the form offers a number field, and an operator who
    types 0 meant "as often as possible", not "fail". The bounds are reported back in
    the same response, so the panel shows what was actually stored.
    """
    enabled = payload.get("enabled", current.enabled)
    mode = payload.get("mode", current.mode)
    # An unreadable time keeps the stored one, for the same reason the numbers are
    # clamped: the panel must not be able to fail a save, and what it shows afterwards
    # is what was actually kept.
    daily_time = parse_daily_time(payload.get("daily_time", current.daily_time))
    return replace(
        current,
        enabled=bool(enabled),
        mode=mode if mode in _MODES else current.mode,
        daily_time=daily_time or current.daily_time,
        interval_hours=_clamp(
            payload.get("interval_hours", current.interval_hours),
            MIN_INTERVAL_HOURS,
            MAX_INTERVAL_HOURS,
            current.interval_hours,
        ),
        keep_per_world=_clamp(
            payload.get("keep_per_world", current.keep_per_world),
            MIN_KEEP,
            MAX_KEEP,
            current.keep_per_world,
        ),
    )


class ScheduleStore:
    """The backup schedule on disk, written the way the credentials file is.

    Its own file rather than a field on ``ManagerState``: that one holds the admin
    password hash and the session secret at mode 0600, and its docstring is about
    authenticating the single admin. A backup interval is not a secret and has no
    business sharing a file whose only job is credentials.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    def load(self) -> BackupSchedule:
        """The stored schedule, or the defaults.

        Anything unreadable or malformed reads as the defaults and says so in the log.
        A corrupt settings file must not be able to stop the manager booting, and the
        defaults -- on, daily, keep seven -- are the behaviour that was asked for.
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return BackupSchedule()
        except (OSError, ValueError) as exc:
            log.warning(
                "Could not read the backup schedule at %s (%s); using the defaults.",
                self.path,
                exc,
            )
            return BackupSchedule()
        if not isinstance(raw, dict):
            log.warning("The backup schedule at %s is not an object; using the defaults.", self.path)
            return BackupSchedule()

        last_run = raw.get("last_run_at")
        try:
            last_run = float(last_run) if last_run is not None else None
        except (TypeError, ValueError):
            last_run = None
        # A file written before the daily mode existed carries neither key, and reads
        # back as the interval schedule it was. There is nothing to migrate.
        mode = raw.get("mode")
        return BackupSchedule(
            enabled=bool(raw.get("enabled", True)),
            mode=mode if mode in _MODES else MODE_INTERVAL,
            daily_time=parse_daily_time(raw.get("daily_time")) or DEFAULT_DAILY_TIME,
            interval_hours=_clamp(
                raw.get("interval_hours"), MIN_INTERVAL_HOURS, MAX_INTERVAL_HOURS, DEFAULT_INTERVAL_HOURS
            ),
            keep_per_world=_clamp(raw.get("keep_per_world"), MIN_KEEP, MAX_KEEP, DEFAULT_KEEP),
            last_run_at=last_run,
            last_error=str(raw.get("last_error") or ""),
        )

    def save(self, schedule: BackupSchedule) -> None:
        """Write it atomically: temp file, fsync, rename, fsync the directory.

        The same discipline as the credentials file. A half-written schedule that read
        back as "never run" would fire a backup on the next tick, which is the one
        outcome a power cut should not cause.
        """
        # Deliberately NOT mkdir(parents=True): the state directory is a mounted
        # volume that already exists, and a missing one means this is not the
        # environment the manager thinks it is. Creating the whole path anyway is how
        # a stray default ends up writing to the filesystem root.
        if not self.path.parent.is_dir():
            raise BackupError(
                f"The manager's state directory {self.path.parent} does not exist, so "
                "the backup schedule cannot be saved. Run `docker compose up -d` on "
                "the server. The setting was not changed."
            )
        staging = self.path.with_name("." + self.path.name + ".part")
        payload = json.dumps(schedule.as_dict(), indent=2, sort_keys=True) + "\n"
        try:
            with open(staging, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(staging, self.path)
            fsync_directory(self.path.parent)
        except OSError as exc:
            try:
                staging.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best effort
                pass
            raise BackupError(
                f"Could not save the backup schedule ({exc}). The setting was not changed."
            ) from exc


class BackupStore:
    """Reads, deletes, restores and prunes the archives in the backups folder."""

    def __init__(self, world_store: WorldStore):
        # The world store owns both directories and the rules about them, so this
        # borrows it rather than taking paths of its own and drifting from it.
        self.worlds = world_store

    @property
    def root(self) -> Path:
        return self.worlds.backups_dir

    # ------------------------------------------------------------------ listing

    def entries(self) -> list[BackupEntry]:
        """Every archive in the folder, newest first.

        Sub-directories are ignored: the image does not recurse and neither does this,
        so a folder somebody made in there is left entirely alone.
        """
        try:
            found = list(os.scandir(self.root))
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise BackupError(
                f"Could not read the backups folder {self.root} ({exc})."
            ) from exc

        entries: list[BackupEntry] = []
        for entry in found:
            # A dotted name is a half-written archive: `backup()` stages as
            # `.NAME.part` and renames. Listing one would offer a Restore of a file
            # that is still being written.
            if entry.name.startswith("."):
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                stat_result = entry.stat(follow_symlinks=False)
            except OSError:  # pragma: no cover - vanished mid-scan
                continue
            kind, world = self._classify(entry.name)
            contains: tuple[str, ...] = ()
            if kind == KIND_GAME and entry.name.lower().endswith(".zip"):
                contains = self._worlds_inside(Path(entry.path))
            entries.append(
                BackupEntry(
                    name=entry.name,
                    world=world,
                    kind=kind,
                    size_bytes=stat_result.st_size,
                    taken_at=stat_result.st_mtime,
                    contains=contains,
                )
            )
        entries.sort(key=lambda item: (-item.taken_at, item.name))
        return entries

    @staticmethod
    def _worlds_inside(path: Path) -> tuple[str, ...]:
        """Which worlds a game archive holds, from its file list alone.

        Only the zip's central directory is read; nothing is extracted. An archive
        that cannot be read -- corrupt, truncated, or carrying an entry the upload
        path refuses -- lists as holding nothing nameable rather than failing the
        whole list, because it is still a file the operator may want to delete.
        """
        try:
            with zipfile.ZipFile(path) as archive:
                return world_names_in(members_from_zip(archive))
        except (zipfile.BadZipFile, OSError, ValueError, WorldError):
            return ()

    @staticmethod
    def _classify(filename: str) -> tuple[str, str]:
        """Which of the three writers made this, and which world it holds.

        The world is read out of the manager's own names, where the format is known.
        The game's names are not parsed for one: ``worlds-20260915-1200.zip`` is a
        timestamp, not a world, and inventing a world name from it would put a wrong
        one in the table.
        """
        for prefix, kind in _PREFIXES.items():
            if filename.startswith(prefix):
                stem = filename[len(prefix):]
                for suffix in (".zip",):
                    if stem.lower().endswith(suffix):
                        stem = stem[: -len(suffix)]
                # `<world>-<stamp>` or `<world>-<stamp>-<n>`, where the stamp is
                # `YYYYmmdd-HHMMSS`. Matched as that exact shape rather than by peeling
                # trailing numbers off: a world may end in one of its own -- `Base-2`,
                # `Fjord-2024` -- and peeling took those with it, which left retention
                # unable to recognise the world it had just backed up and restore
                # landing under a name the server does not load. The world part is
                # greedy, so a world that itself looks like a stamp keeps it.
                match = _STAMPED_RE.match(stem)
                return kind, match.group("world") if match else stem
        return KIND_GAME, ""

    def find(self, name: str) -> BackupEntry | None:
        safe = self._safe_archive_name(name)
        for entry in self.entries():
            if entry.name == safe:
                return entry
        return None

    # ----------------------------------------------------------------- deleting

    def delete(self, name: str) -> BackupEntry:
        """Remove one archive from the backups folder. Returns what went.

        The game's own archives included: the image prunes them only when it takes a
        new one, so they can outlive ``BACKUPS_MAX_AGE`` indefinitely, and a row the
        operator can see but not remove is the panel failing at its one job. A name
        that resolves outside the folder, or a link, is still not a backup at all.
        """
        entry = self.find(name)
        if entry is None:
            raise BackupError(
                f"There is no backup called {name!r} any more. Refresh the list."
            )
        target = self._resolved(entry.name)
        try:
            if target.is_symlink():
                raise BackupError(
                    f"{entry.name} is a link, not a backup, so it will not be "
                    "deleted -- a link can point anywhere."
                )
            target.unlink()
        except BackupError:
            raise
        except OSError as exc:
            raise BackupError(f"Could not delete {entry.name} ({exc}). Nothing was removed.") from exc
        log.info("Backup %s deleted from %s.", entry.name, self.root)
        return entry

    # ---------------------------------------------------------------- restoring

    def restore(self, name: str, *, target_name: str = "", overwrite: bool = False) -> World:
        """Write one archive back out as a world.

        The archive's bytes go through the upload path -- the same entry-name checks,
        the same symlink and traversal refusals, the same size caps and the same
        stage-then-rename write. There is deliberately no second way to write a world.

        Without ``overwrite`` a name already on the volume is refused, which makes the
        default non-destructive: restoring alongside the current world and switching
        with Load costs nothing and loses nothing. ``overwrite`` replaces the world
        instead, and the caller is responsible for having asked first.
        """
        entry = self.find(name)
        if entry is None:
            raise BackupError(f"There is no backup called {name!r} any more. Refresh the list.")
        if not entry.restorable:
            raise BackupError(
                f"{entry.name} is not a .zip, so the dashboard cannot read it back as "
                "a world. The game server writes some of its own backups as loose "
                "files; restore one of those on the host."
            )

        path = self._resolved(entry.name)
        if path.is_symlink():
            raise BackupError(
                f"{entry.name} is a link, not a backup, so it will not be restored."
            )

        # The name to land under: what was asked for, else the world the archive was
        # taken from, else whatever the archive itself says it holds.
        wanted = (target_name or entry.world or "").strip()
        try:
            with zipfile.ZipFile(path) as archive:
                members = members_from_zip(archive)
                plan = plan_upload(members, requested_name=wanted)
                return self.worlds.place(plan, overwrite=overwrite)
        except zipfile.BadZipFile as exc:
            raise BackupError(
                f"{entry.name} is not a readable zip ({exc}). Nothing was written."
            ) from exc
        except WorldError:
            # Already phrased for the operator by the upload path, collisions
            # included -- re-wording it here would only make it vaguer.
            raise
        except OSError as exc:
            raise BackupError(
                f"Could not read {entry.name} ({exc}). Nothing was written."
            ) from exc

    # ------------------------------------------------------------------ pruning

    def prune(self, world: str, keep: int) -> list[str]:
        """Drop the oldest SCHEDULED- archives of one world past the newest ``keep``.

        Narrow on purpose, and the narrowness is the safety: it only ever considers
        archives this module's own timer wrote, and only for the one world just backed
        up. A manual backup is never a candidate however old it is, and neither is one
        of the game's.

        Called only after a successful write, so a failed backup can never be the
        thing that deletes history.
        """
        safe = sanitised_name(world)
        mine = [
            entry
            for entry in self.entries()
            if entry.kind == KIND_SCHEDULED and entry.world == safe
        ]
        # entries() is newest-first, so anything past `keep` is what to drop.
        doomed = mine[max(0, keep):]
        removed: list[str] = []
        for entry in doomed:
            try:
                target = self._resolved(entry.name)
                if target.is_symlink():
                    continue
                target.unlink()
            except (OSError, BackupError) as exc:
                # One unremovable file must not abort the rest, and must never fail
                # the backup that has already been written successfully.
                log.warning("Could not prune old backup %s: %s", entry.name, exc)
                continue
            removed.append(entry.name)
        if removed:
            log.info(
                "Pruned %d old scheduled backup(s) of %r: %s",
                len(removed),
                safe,
                ", ".join(removed),
            )
        return removed

    # ------------------------------------------------------------------- timer

    def run_scheduled(self, world: str, keep: int) -> tuple[str, list[str]]:
        """Back up one world as the timer, then prune. Returns (archive, pruned)."""
        made = self.worlds.backup(world, prefix=SCHEDULED_PREFIX)
        pruned = self.prune(world, keep)
        return made.name, pruned

    # ------------------------------------------------------------------ helpers

    def _safe_archive_name(self, raw: str) -> str:
        """One path segment, no traversal, no separators -- or nothing at all.

        Reuses the world-name rule rather than inventing a second one, then allows the
        dot an archive's extension needs. ``sanitised_name`` is what refuses ``..``,
        a slash, a backslash and a leading dash. It is given the archive cap, not the
        world's: an archive's name is a world's name plus a prefix and a stamp, so the
        world's cap refused names this module had itself written -- and every backup
        of a long-named world became impossible to delete or restore.

        Whatever it refuses is re-raised as ``BackupError``. The routes here catch that
        and nothing else, so a ``WorldError`` reaching them is a 500 rather than a
        sentence the operator can act on.
        """
        candidate = (raw or "").strip()
        if not candidate:
            raise BackupError("No backup was named.")
        stem, dot, extension = candidate.rpartition(".")
        if not dot:
            raise BackupError(f"{candidate!r} is not a backup file name.")
        if extension.lower() not in {"zip", "db", "fwl", "tgz", "gz"}:
            raise BackupError(f"{candidate!r} is not a backup file name.")
        try:
            sanitised_name(stem, what="backup name", max_length=MAX_ARCHIVE_NAME_LENGTH)
        except WorldError as exc:
            raise BackupError(str(exc)) from exc
        return candidate

    def _resolved(self, name: str) -> Path:
        """``name`` inside the backups folder, refusing anything that escapes it."""
        root = self.root.resolve()
        target = (root / name).resolve()
        if target != root and root not in target.parents:
            raise BackupError(f"{name!r} is not inside the backups folder.")
        return target


def next_wake(schedule: BackupSchedule, now: float | None = None) -> float:
    """Seconds to sleep before the timer should look again.

    Never longer than one tick, so a schedule changed in the panel takes effect within
    a minute rather than at the end of a 24-hour sleep the loop is already inside.
    """
    if now is None:
        now = time.time()
    due = schedule.due_at()
    if due is None:
        return float(TICK_SECONDS)
    return float(max(0.0, min(TICK_SECONDS, due - now)))
