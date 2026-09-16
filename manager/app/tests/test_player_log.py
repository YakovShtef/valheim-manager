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


from app.player_log import PlayerUpdate, SessionTracker

A = "76561198012345678"
B = "76561198000000001"


def join(pid: str, epoch: float = 100.0):
    return JoinEvent(platform_id=pid, platform="steam", epoch=epoch)


def leave(pid: str, epoch: float = 200.0):
    return LeaveEvent(platform_id=pid, platform="steam", epoch=epoch)


def test_join_emits_an_update_with_no_name():
    tracker = SessionTracker()
    assert tracker.apply(join(A)) == PlayerUpdate(
        platform_id=A, platform="steam", epoch=100.0, name=None
    )


def test_leave_emits_an_update_so_last_seen_moves():
    tracker = SessionTracker()
    tracker.apply(join(A))
    assert tracker.apply(leave(A)) == PlayerUpdate(
        platform_id=A, platform="steam", epoch=200.0, name=None
    )


def test_name_attaches_when_one_player_is_connected():
    tracker = SessionTracker()
    tracker.apply(join(A))
    assert tracker.apply(NameEvent(name="Ragnar", epoch=150.0)) == PlayerUpdate(
        platform_id=A, platform="steam", epoch=150.0, name="Ragnar"
    )


def test_name_attaches_to_the_only_unnamed_player():
    tracker = SessionTracker()
    tracker.apply(join(A))
    tracker.apply(NameEvent(name="Ragnar", epoch=150.0))
    tracker.apply(join(B, epoch=160.0))
    assert tracker.apply(NameEvent(name="Astrid", epoch=170.0)) == PlayerUpdate(
        platform_id=B, platform="steam", epoch=170.0, name="Astrid"
    )


def test_name_attaches_to_nobody_when_two_are_unnamed():
    """Two people loading in together is an ordinary evening, not an edge case.
    A name against the wrong player is worse than no name."""
    tracker = SessionTracker()
    tracker.apply(join(A))
    tracker.apply(join(B))
    assert tracker.apply(NameEvent(name="Ragnar", epoch=150.0)) is None


def test_a_respawn_updates_the_name_of_a_lone_player():
    """`Got character ZDOID` fires again on death and respawn."""
    tracker = SessionTracker()
    tracker.apply(join(A))
    tracker.apply(NameEvent(name="Ragnar", epoch=150.0))
    assert tracker.apply(NameEvent(name="Ragnar", epoch=300.0)) == PlayerUpdate(
        platform_id=A, platform="steam", epoch=300.0, name="Ragnar"
    )


def test_a_name_with_nobody_connected_is_dropped():
    assert SessionTracker().apply(NameEvent(name="Ragnar", epoch=150.0)) is None


def test_leave_for_an_unknown_id_still_counts_as_a_sighting():
    """Backfill can start mid-session, so the join may predate the log we can read."""
    tracker = SessionTracker()
    assert tracker.apply(leave(A)) == PlayerUpdate(
        platform_id=A, platform="steam", epoch=200.0, name=None
    )
