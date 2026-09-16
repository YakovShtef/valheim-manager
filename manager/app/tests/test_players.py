"""The roster: identity and recency for everyone who has ever joined.

Membership of the admin / ban / permit lists is deliberately NOT here. Those live in
the files on /config, which are the single source of truth; caching them would give
the table and the raw editors two copies of one fact and let them drift.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from app.docker_control import DockerControlError, LogLine
from app.player_log import PlayerUpdate, SessionTracker
from app.players import WATCH_INTERVAL_SECONDS, Player, PlayerStore, harvest_players

A = "76561198086248026"

JOIN_MSG = "supervisord: valheim-server 09/16/2026 07:45:28: Got connection SteamID 76561198086248026"
NAME_MSG = "supervisord: valheim-server 09/16/2026 07:45:48: Got character ZDOID from Loped : -12:5"


def update(pid=A, epoch=100.0, name=None, platform="steam"):
    return PlayerUpdate(platform_id=pid, platform=platform, epoch=epoch, name=name)


def test_an_empty_store_reads_as_no_players(tmp_path):
    assert PlayerStore(tmp_path / "players.json").load() == {}


def test_a_first_sighting_creates_a_row(tmp_path):
    store = PlayerStore(tmp_path / "players.json")
    players = store.apply([update(epoch=100.0)], world="Midgard")
    assert players[A] == Player(
        platform_id=A,
        platform="steam",
        name=None,
        first_seen=100.0,
        last_seen=100.0,
        last_world="Midgard",
    )


def test_a_later_sighting_moves_last_seen_but_not_first_seen(tmp_path):
    store = PlayerStore(tmp_path / "players.json")
    store.apply([update(epoch=100.0)], world="Midgard")
    players = store.apply([update(epoch=500.0)], world="Midgard")
    assert players[A].first_seen == 100.0
    assert players[A].last_seen == 500.0


def test_replaying_the_same_update_is_idempotent(tmp_path):
    """Startup backfill replays the whole log unconditionally; that must be safe."""
    store = PlayerStore(tmp_path / "players.json")
    first = store.apply([update(epoch=100.0)], world="Midgard")
    second = store.apply([update(epoch=100.0)], world="Midgard")
    assert first == second


def test_a_name_is_recorded_and_later_updated(tmp_path):
    store = PlayerStore(tmp_path / "players.json")
    store.apply([update(epoch=100.0, name="Loped")], world="Midgard")
    players = store.apply([update(epoch=200.0, name="Loped the Red")], world="Midgard")
    assert players[A].name == "Loped the Red"


def test_a_nameless_update_does_not_erase_a_known_name(tmp_path):
    """Every join is nameless; the name arrives ~20s later. A later join must not
    wipe the name learned in an earlier session."""
    store = PlayerStore(tmp_path / "players.json")
    store.apply([update(epoch=100.0, name="Loped")], world="Midgard")
    players = store.apply([update(epoch=200.0, name=None)], world="Midgard")
    assert players[A].name == "Loped"


def test_the_store_survives_a_round_trip(tmp_path):
    path = tmp_path / "players.json"
    PlayerStore(path).apply([update(epoch=100.0, name="Loped")], world="Midgard")
    assert PlayerStore(path).load()[A].name == "Loped"


def test_a_corrupt_store_reads_as_empty_rather_than_crashing(tmp_path):
    path = tmp_path / "players.json"
    path.write_text("{not json", encoding="utf-8")
    assert PlayerStore(path).load() == {}


def test_the_file_is_json_keyed_by_platform_id(tmp_path):
    path = tmp_path / "players.json"
    PlayerStore(path).apply([update(epoch=100.0)], world="Midgard")
    assert list(json.loads(path.read_text(encoding="utf-8"))) == [A]


def test_save_handles_mkstemp_failure_gracefully(tmp_path, caplog):
    """A failure to create the temp file is logged and does not raise."""
    store = PlayerStore(tmp_path / "players.json")
    players = {A: Player(
        platform_id=A,
        platform="steam",
        name="Loped",
        first_seen=100.0,
        last_seen=100.0,
        last_world="Midgard",
    )}

    # Monkeypatch tempfile.mkstemp to raise OSError
    with patch("tempfile.mkstemp", side_effect=OSError("Permission denied")):
        # This should not raise; it should log and return
        store.save(players)

    # Verify the warning was logged
    assert "Could not write the roster" in caplog.text
    assert "Permission denied" in caplog.text


class FakeControl:
    """Stands in for DockerControl: records the `since` it was polled with."""

    def __init__(self, batches):
        self.batches = list(batches)
        self.since_calls = []

    def fetch_logs(self, *, since=None, tail="all"):
        self.since_calls.append(since)
        return self.batches.pop(0) if self.batches else []


def test_a_harvest_records_the_player_and_the_name(tmp_path):
    control = FakeControl([[LogLine(100.0, "r", JOIN_MSG), LogLine(150.0, "r", NAME_MSG)]])
    store = PlayerStore(tmp_path / "players.json")
    harvest_players(control, SessionTracker(), store, since=0.0, world="Midgard")
    assert store.load()[A].name == "Loped"


def test_a_harvest_returns_the_epoch_to_poll_from_next(tmp_path):
    control = FakeControl([[LogLine(100.0, "r", JOIN_MSG), LogLine(150.0, "r", NAME_MSG)]])
    store = PlayerStore(tmp_path / "players.json")
    since = harvest_players(control, SessionTracker(), store, since=0.0, world="Midgard")
    assert since == 150.0


def test_an_empty_read_leaves_since_where_it_was(tmp_path):
    control = FakeControl([[]])
    store = PlayerStore(tmp_path / "players.json")
    assert harvest_players(control, SessionTracker(), store, since=42.0, world=None) == 42.0


def test_the_tracker_persists_across_harvests(tmp_path):
    """A join in one poll and its name in the next must still correlate."""
    control = FakeControl([[LogLine(100.0, "r", JOIN_MSG)], [LogLine(150.0, "r", NAME_MSG)]])
    store = PlayerStore(tmp_path / "players.json")
    tracker = SessionTracker()
    since = harvest_players(control, tracker, store, since=0.0, world="Midgard")
    harvest_players(control, tracker, store, since=since, world="Midgard")
    assert store.load()[A].name == "Loped"


def test_a_docker_error_does_not_escape(tmp_path):
    """A watcher that raises would take down the only always-on task in the app."""

    class Broken:
        def fetch_logs(self, *, since=None, tail="all"):
            raise DockerControlError("nope", "detail")

    store = PlayerStore(tmp_path / "players.json")
    assert harvest_players(Broken(), SessionTracker(), store, since=7.0, world=None) == 7.0


def test_the_interval_is_a_sane_poll_rate():
    assert 5 <= WATCH_INTERVAL_SECONDS <= 60
