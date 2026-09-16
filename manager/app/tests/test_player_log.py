"""Parsing player events out of the game container's log.

Every fixture line here was captured from the live server on 2026-09-16. Invented
lines would only prove the parser matches its author's guess.
"""

from __future__ import annotations

from app.docker_control import LogLine
from app.player_log import JoinEvent, LeaveEvent, NameEvent, parse_line

JOIN = (
    "Sep 16 07:45:28 supervisord: valheim-server 09/16/2026 07:45:28: "
    "Got connection SteamID 76561198012345678"
)
NAME = (
    "Sep 16 07:45:48 supervisord: valheim-server 09/16/2026 07:45:48: "
    "Got character ZDOID from Ragnar : -1297766619:5"
)
LEAVE = (
    "Sep 16 07:45:51 supervisord: valheim-server 09/16/2026 07:45:51: "
    "Closing socket 76561198012345678"
)
HANDSHAKE = (
    "Sep 16 07:45:28 supervisord: valheim-server 09/16/2026 07:45:28: "
    "Got handshake from client 76561198012345678"
)
UPDATER = (
    "Sep 16 07:45:44 supervisord: valheim-updater  Update state (0x5) "
    "verifying install, progress: 40.16 (871065970 / 2168738369)"
)


def line(message: str, epoch: float = 1_758_008_728.0) -> LogLine:
    return LogLine(epoch=epoch, raw=f"2026-09-16T07:45:28Z {message}", message=message)


def test_join_line_yields_a_join_event():
    event = parse_line(line(JOIN))
    assert event == JoinEvent(
        platform_id="76561198012345678", platform="steam", epoch=1_758_008_728.0
    )


def test_leave_line_yields_a_leave_event():
    event = parse_line(line(LEAVE))
    assert event == LeaveEvent(
        platform_id="76561198012345678", platform="steam", epoch=1_758_008_728.0
    )


def test_name_line_yields_a_name_event_without_an_id():
    assert parse_line(line(NAME)) == NameEvent(name="Ragnar", epoch=1_758_008_728.0)


def test_handshake_is_not_an_event():
    """Redundant with the connection line; counting it would double every join."""
    assert parse_line(line(HANDSHAKE)) is None


def test_updater_output_is_not_an_event():
    """The updater interleaves with game output in the same stream."""
    assert parse_line(line(UPDATER)) is None


def test_a_name_containing_spaces_survives():
    message = "supervisord: valheim-server 09/16/2026 07:45:48: Got character ZDOID from Bjorn the Red : -12:5"
    assert parse_line(line(message)) == NameEvent(name="Bjorn the Red", epoch=1_758_008_728.0)
