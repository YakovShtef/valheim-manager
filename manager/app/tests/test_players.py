"""The roster: identity and recency for everyone who has ever joined.

Membership of the admin / ban / permit lists is deliberately NOT here. Those live in
the files on /config, which are the single source of truth; caching them would give
the table and the raw editors two copies of one fact and let them drift.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.docker_control import DockerControlError, LogLine
from app.main import create_app
from app.permission_lists import PermissionListError
from app.player_log import PlayerUpdate, SessionTracker
from app.players import WATCH_INTERVAL_SECONDS, Player, PlayerStore, harvest_players

# `build_config`/`build_control` need a temp env file and a fake engine; pytest
# resolves a fixture's own dependencies by name, so they have to come across too.
from app.tests.test_edge_cases import (  # noqa: F401  (fixtures)
    build_config,
    build_control,
    env_file,
    fake_docker,
    login,
)

A = "76561198012345678"
A_FILE_ID = "V_76561198012345678"

JOIN_MSG = "supervisord: valheim-server 09/16/2026 07:45:28: Got connection SteamID 76561198012345678"
NAME_MSG = "supervisord: valheim-server 09/16/2026 07:45:48: Got character ZDOID from Ragnar : -12:5"


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
    store.apply([update(epoch=100.0, name="Ragnar")], world="Midgard")
    players = store.apply([update(epoch=200.0, name="Ragnar the Red")], world="Midgard")
    assert players[A].name == "Ragnar the Red"


def test_a_nameless_update_does_not_erase_a_known_name(tmp_path):
    """Every join is nameless; the name arrives ~20s later. A later join must not
    wipe the name learned in an earlier session."""
    store = PlayerStore(tmp_path / "players.json")
    store.apply([update(epoch=100.0, name="Ragnar")], world="Midgard")
    players = store.apply([update(epoch=200.0, name=None)], world="Midgard")
    assert players[A].name == "Ragnar"


def test_the_store_survives_a_round_trip(tmp_path):
    path = tmp_path / "players.json"
    PlayerStore(path).apply([update(epoch=100.0, name="Ragnar")], world="Midgard")
    assert PlayerStore(path).load()[A].name == "Ragnar"


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
        name="Ragnar",
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
    assert store.load()[A].name == "Ragnar"


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
    assert store.load()[A].name == "Ragnar"


def test_a_docker_error_does_not_escape(tmp_path):
    """A watcher that raises would take down the only always-on task in the app."""

    class Broken:
        def fetch_logs(self, *, since=None, tail="all"):
            raise DockerControlError("nope", "detail")

    store = PlayerStore(tmp_path / "players.json")
    assert harvest_players(Broken(), SessionTracker(), store, since=7.0, world=None) == 7.0


def test_the_interval_is_a_sane_poll_rate():
    assert 5 <= WATCH_INTERVAL_SECONDS <= 60


# ---------------------------------------------------------- GET /api/players


@pytest.fixture
def client_with_roster(tmp_path, env_file, fake_docker):
    """The real app, with a roster and the three list files seeded underneath it.

    The players state directory exists here, so the background watcher DOES start
    and polls the fake Docker engine -- same as it would in production. The fake
    engine has no log lines queued, so a poll finds nothing and leaves the roster
    this fixture seeds directly untouched.
    """
    state = tmp_path / "state"
    state.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    (config_dir / "adminlist.txt").write_text(f"// header\n{A_FILE_ID}\n", encoding="utf-8")
    (config_dir / "bannedlist.txt").write_text(
        "// header\nV_76561198000000001\n", encoding="utf-8"
    )

    players_file = state / "players.json"
    PlayerStore(players_file).apply(
        [PlayerUpdate(platform_id=A, platform="steam", epoch=100.0, name="Ragnar")],
        world="Midgard",
    )

    config = build_config(
        env_file,
        players_file=str(players_file),
        valheim_config_dir=str(config_dir),
    )
    control = build_control(config, fake_docker)
    app = create_app(config=config, controller=control)
    with TestClient(app) as client:
        login(client)
        yield client, tmp_path


def test_the_roster_endpoint_merges_the_files_with_the_store(client_with_roster):
    client, tmp = client_with_roster
    body = client.get("/api/players").json()
    rows = {row["id"]: row for row in body["players"]}
    assert rows[A_FILE_ID]["is_admin"] is True
    assert rows[A_FILE_ID]["seen"] is True


def test_an_id_only_in_a_file_still_gets_a_row(client_with_roster):
    """Someone made admin before this feature existed has never joined, so has no
    sighting -- but must still be visible and removable."""
    client, tmp = client_with_roster
    rows = {row["id"]: row for row in client.get("/api/players").json()["players"]}
    assert rows["V_76561198000000001"]["seen"] is False
    assert rows["V_76561198000000001"]["is_banned"] is True


def test_membership_is_read_from_the_file_not_the_store(client_with_roster):
    """Edit the file underneath the app; the next read must reflect it.

    Two requests on purpose: a single request made only after the edit would also
    pass under a regression that computes membership once and memoises it (e.g. an
    `lru_cache` on `_roster_payload`), since that first computation would already
    happen after the edit. Reading True *before* the edit rules that out.
    """
    client, tmp = client_with_roster
    before = {row["id"]: row for row in client.get("/api/players").json()["players"]}
    assert before[A_FILE_ID]["is_admin"] is True

    (tmp / "config" / "adminlist.txt").write_text("// header\n", encoding="utf-8")

    after = {row["id"]: row for row in client.get("/api/players").json()["players"]}
    assert after[A_FILE_ID]["is_admin"] is False


def test_an_unreadable_list_file_is_a_refusal_not_a_crash(client_with_roster):
    """A `PermissionListError` (e.g. a permission problem on the /config mount) must
    surface as this app's own `{"error": ...}` shape, not an unhandled traceback.

    `chmod` cannot produce a real permission failure reliably on Windows CI, so the
    failure is induced directly: monkeypatch `PermissionLists.read` to raise the same
    exception a real unreadable file would.
    """
    client, tmp = client_with_roster
    message = "Could not read /config/adminlist.txt: [Errno 13] Permission denied"
    with patch(
        "app.main.PermissionLists.read",
        side_effect=PermissionListError(message),
    ):
        response = client.get("/api/players")

    assert response.status_code == 500, response.text
    assert response.json()["error"] == message
