"""The roster of everyone who has ever joined, persisted across restarts.

Its own file, not a field on ``ManagerState``: that one is mode 0600 and holds the
admin password hash and the session secret. ``backups.py`` already set this precedent
for the backup schedule -- non-secret data gets its own file.

What is deliberately NOT stored here: whether a player is an admin, banned or
permitted. Those live in the three files on /config and are read fresh on every
render. One copy of the fact means the roster table and the raw file editors cannot
disagree about it.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .player_log import PlayerUpdate, SessionTracker, parse_line
from .state_store import fsync_directory

log = logging.getLogger(__name__)

PLAYERS_MODE = 0o644


@dataclass
class Player:
    platform_id: str
    platform: str
    name: str | None
    first_seen: float
    last_seen: float
    last_world: str | None


class PlayerStore:
    """Loads and atomically saves the roster."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    def load(self) -> dict[str, Player]:
        """The stored roster, or empty.

        Anything unreadable or malformed reads as empty and says so in the log. A
        corrupt roster must not be able to stop the manager booting; the worst case
        is that the watcher rebuilds what the container log still holds.
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("Could not read the roster at %s (%s); starting empty.", self.path, exc)
            return {}
        if not isinstance(raw, dict):
            log.warning("The roster at %s is not an object; starting empty.", self.path)
            return {}
        players: dict[str, Player] = {}
        for pid, row in raw.items():
            if not isinstance(row, dict):
                continue
            try:
                players[pid] = Player(
                    platform_id=str(row.get("platform_id", pid)),
                    platform=str(row.get("platform", "unknown")),
                    name=row.get("name") or None,
                    first_seen=float(row.get("first_seen", 0.0)),
                    last_seen=float(row.get("last_seen", 0.0)),
                    last_world=row.get("last_world") or None,
                )
            except (TypeError, ValueError):
                log.warning("Skipping a malformed roster row for %s.", pid)
        return players

    def apply(self, updates: Iterable[PlayerUpdate], *, world: str | None) -> dict[str, Player]:
        """Fold updates into the roster and save. Returns the new roster.

        Idempotent on ``platform_id``: replaying an update the roster has already
        seen moves nothing, which is what makes the watcher's unconditional startup
        backfill safe to run on every boot.
        """
        players = self.load()
        changed = False
        for item in updates:
            existing = players.get(item.platform_id)
            if existing is None:
                players[item.platform_id] = Player(
                    platform_id=item.platform_id,
                    platform=item.platform,
                    name=item.name,
                    first_seen=item.epoch,
                    last_seen=item.epoch,
                    last_world=world,
                )
                changed = True
                continue
            if item.epoch > existing.last_seen:
                existing.last_seen = item.epoch
                existing.last_world = world
                changed = True
            if item.epoch < existing.first_seen:
                existing.first_seen = item.epoch
                changed = True
            # A nameless update never erases a name: every join is nameless, and the
            # name only arrives once the player has finished loading in.
            if item.name and item.name != existing.name:
                existing.name = item.name
                changed = True
        if changed:
            self.save(players)
        return players

    def save(self, players: dict[str, Player]) -> None:
        """Temp file, then ``os.replace``, so a reader never sees half a roster."""
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {pid: asdict(player) for pid, player in players.items()},
            indent=2,
            sort_keys=True,
        )
        try:
            fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=".players-", suffix=".tmp")
        except OSError as exc:
            log.warning("Could not write the roster at %s: %s", self.path, exc)
            return
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, PLAYERS_MODE)
            os.replace(tmp_name, self.path)
            fsync_directory(parent)
        except OSError as exc:
            try:
                os.unlink(tmp_name)
            except OSError:  # pragma: no cover - already gone
                pass
            log.warning("Could not write the roster at %s: %s", self.path, exc)


# Short enough that the Players tab's "online now" list keeps up with joins and
# leaves; each tick is one `since`-bounded log read, so it costs next to nothing.
WATCH_INTERVAL_SECONDS = 5


def harvest_players(
    control,
    tracker: SessionTracker,
    store: PlayerStore,
    *,
    since: float,
    world: str | None,
) -> float:
    """One poll of the container log, folded into the roster.

    Returns the epoch to poll from next -- the newest line seen, or ``since``
    unchanged when nothing new arrived.

    Swallows every exception on purpose. This runs in the only always-on background
    task besides the backup timer; letting a transient Docker error escape would kill
    the watcher for the lifetime of the process, and the next tick would have
    recovered on its own.
    """
    try:
        lines = control.fetch_logs(since=since or None, tail="all")
    except Exception as exc:  # noqa: BLE001 - deliberate: see docstring
        log.warning("Could not read the game log for the roster: %s", exc)
        return since
    if not lines:
        return since
    updates = []
    newest = since
    for line in lines:
        newest = max(newest, line.epoch)
        event = parse_line(line)
        if event is None:
            continue
        update = tracker.apply(event)
        if update is not None:
            updates.append(update)
    if updates:
        try:
            store.apply(updates, world=world)
        except Exception as exc:  # noqa: BLE001 - see docstring
            log.warning("Could not update the roster: %s", exc)
    return newest



class LivePlayers:
    """Who is connected right now, for the CURRENT run of the game container.

    The roster remembers everyone; this answers a narrower question. It is fed by the
    same log, but scoped to one run: a run ends when the server stops or its container
    is replaced, and a run that ended badly -- a crash, a kill -- never logs the
    leaves. So the session tracker is thrown away at every run boundary rather than
    trusted to drain, and nobody is online while the server is not running.

    The first tick still backfills the roster from the whole log, earlier runs
    included, exactly as the watcher always has; only presence is run-scoped.

    ``tick`` runs on a worker thread and ``online`` is read from request threads, so
    both take the lock.
    """

    def __init__(self, control, store: PlayerStore):
        self.control = control
        self.store = store
        self._tracker = SessionTracker()
        self._since = 0.0
        self._run_key: tuple[str, str] | None = None
        self._running = False
        self._backfilled = False
        self._lock = threading.Lock()

    def tick(self, *, world: str | None) -> None:
        """One poll. Never raises: it runs in the app's always-on background task."""
        try:
            run = self.control.current_run()
        except Exception as exc:  # noqa: BLE001 - an outage must not kill the watcher
            log.warning("Could not read the game container's state: %s", exc)
            with self._lock:
                self._running = False
            return
        with self._lock:
            key = run.key if run is not None else None
            if key != self._run_key:
                if not self._backfilled:
                    harvest_players(
                        self.control, SessionTracker(), self.store, since=0.0, world=world
                    )
                    self._backfilled = True
                self._tracker = SessionTracker()
                if run is not None and run.started_at > 0:
                    self._since = run.started_at
                self._run_key = key
            self._running = bool(run is not None and run.running)
            self._since = harvest_players(
                self.control, self._tracker, self.store, since=self._since, world=world
            )

    def online(self) -> dict[str, float]:
        """Platform id -> when their session began. Empty while the server is down."""
        with self._lock:
            return dict(self._tracker.joined_at) if self._running else {}

__all__ = [
    "LivePlayers",
    "PLAYERS_MODE",
    "Player",
    "PlayerStore",
    "WATCH_INTERVAL_SECONDS",
    "harvest_players",
]
