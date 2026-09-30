"""Player events, parsed out of the game container's log.

The log is the only source of who has played here: vanilla Valheim exposes no RCON
and no query interface. Three line shapes matter, all captured from a live server:

    Got connection SteamID 76561198012345678      -- a join, with identity
    Got character ZDOID from Ragnar : -1297766619:5 -- a name, with NO identity
    Closing socket 76561198012345678              -- a leave, with identity

The name line shares no field with the other two: -1297766619 is a ZDO owner id,
unrelated to the platform id. Attaching a name to a player is therefore inference,
and lives in ``SessionTracker`` rather than here.

Event time comes from ``LogLine.epoch`` -- Docker's own RFC3339 engine timestamp, in
UTC and already parsed. The game's inline "09/16/2026 07:45:28" is deliberately
ignored: it carries no zone, so it cannot be turned into an instant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .docker_control import LogLine

# Unanchored on purpose. A message arrives as
#   "Sep 16 07:45:28 supervisord: valheim-server 09/16/2026 07:45:28: Got connection ..."
# -- a supervisord stamp, a process name and the game's own stamp all precede the text.
_JOIN_RE = re.compile(r"Got connection SteamID (\d+)")
_LEAVE_RE = re.compile(r"Closing socket (\d+)")
# Non-greedy up to " : ", so a display name containing spaces survives intact.
_NAME_RE = re.compile(r"Got character ZDOID from (.+?) : -?\d+:\d+")

STEAM = "steam"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class JoinEvent:
    platform_id: str
    platform: str
    epoch: float


@dataclass(frozen=True)
class LeaveEvent:
    platform_id: str
    platform: str
    epoch: float


@dataclass(frozen=True)
class NameEvent:
    name: str
    epoch: float


PlayerEvent = JoinEvent | LeaveEvent | NameEvent


def parse_line(line: LogLine) -> PlayerEvent | None:
    """One log line to one event, or ``None`` for the overwhelming majority.

    Deliberately not events: ``Got handshake from client`` (redundant with the
    connection line, and counting both would double every join), ``RPC_Disconnect``
    and ``Disposing socket`` (no id on them), and everything the ``valheim-updater``
    process writes into the same stream.
    """
    message = line.message
    match = _JOIN_RE.search(message)
    if match:
        return JoinEvent(platform_id=match.group(1), platform=STEAM, epoch=line.epoch)
    match = _LEAVE_RE.search(message)
    if match:
        return LeaveEvent(platform_id=match.group(1), platform=STEAM, epoch=line.epoch)
    match = _NAME_RE.search(message)
    if match:
        return NameEvent(name=match.group(1).strip(), epoch=line.epoch)
    return None


@dataclass(frozen=True)
class PlayerUpdate:
    """One thing learned about one player, ready for the store to fold in."""

    platform_id: str
    platform: str
    epoch: float
    name: str | None


class SessionTracker:
    """Who is connected right now, so a nameless name line can find its owner.

    The name line carries no platform id, so ownership is inferred from connection
    state. The inference is deliberately conservative: when it cannot be certain, it
    attaches the name to nobody and the row keeps showing the id until a session in
    which that player is alone.
    """

    def __init__(self) -> None:
        # platform id -> the name we have for them this session, or None.
        self.connected: dict[str, str | None] = {}
        self._platforms: dict[str, str] = {}

    def apply(self, event: PlayerEvent) -> PlayerUpdate | None:
        if isinstance(event, JoinEvent):
            self.connected[event.platform_id] = None
            self._platforms[event.platform_id] = event.platform
            return PlayerUpdate(event.platform_id, event.platform, event.epoch, None)

        if isinstance(event, LeaveEvent):
            self.connected.pop(event.platform_id, None)
            # Still a sighting: backfill can begin mid-session, so a leave may be the
            # first and only thing the log tells us about this player.
            return PlayerUpdate(event.platform_id, event.platform, event.epoch, None)

        owner = self._owner_of_a_name()
        if owner is None:
            return None
        self.connected[owner] = event.name
        return PlayerUpdate(owner, self._platforms.get(owner, UNKNOWN), event.epoch, event.name)

    def _owner_of_a_name(self) -> str | None:
        """The one player a name can safely belong to, or ``None``.

        One connected player: it is theirs, named already or not -- that covers the
        respawn case, where the line fires again for someone who already has a name.
        Several connected: only if exactly one of them is still unnamed.
        """
        if len(self.connected) == 1:
            return next(iter(self.connected))
        unnamed = [pid for pid, name in self.connected.items() if name is None]
        if len(unnamed) == 1:
            return unnamed[0]
        return None


__all__ = ["JoinEvent", "LeaveEvent", "NameEvent", "PlayerEvent", "STEAM", "UNKNOWN", "parse_line", "PlayerUpdate", "SessionTracker"]
