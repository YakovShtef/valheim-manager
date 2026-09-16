"""The roster: identity and recency for everyone who has ever joined.

Membership of the admin / ban / permit lists is deliberately NOT here. Those live in
the files on /config, which are the single source of truth; caching them would give
the table and the raw editors two copies of one fact and let them drift.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from app.player_log import PlayerUpdate
from app.players import Player, PlayerStore

A = "76561198086248026"


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
