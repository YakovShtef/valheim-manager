"""The backups folder: naming, retention, restore, and the schedule.

The feature shipped with no automated coverage at all -- it was checked by two
throwaway scripts that died with the session that wrote them. These are the
assertions worth keeping, starting with the two bugs that survived into `main`.

Both bugs are about the *filename* being a contract between two modules:
``worlds.py`` composes ``<prefix><world>-<stamp>.zip`` and ``backups.py`` reads the
world back out of it. Neither failure raises where it is caused, which is why both
are here rather than left to be noticed.
"""

from __future__ import annotations

import json
import time
import zipfile
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.backups import (
    DEFAULT_INTERVAL_HOURS,
    MODE_DAILY,
    MODE_INTERVAL,
    BackupError,
    BackupSchedule,
    BackupStore,
    ScheduleStore,
    schedule_from_payload,
)
from app.main import create_app
from app.settings_store import SettingsStore
from app.worlds import SCHEDULED_PREFIX, WorldError, WorldStore

# `build_config`/`build_control` need a temp env file and a fake engine; pytest
# resolves a fixture's own dependencies by name, so they have to come across too.
from app.tests.test_edge_cases import (  # noqa: F401  (fixtures)
    ADMIN_PASSWORD,
    ADMIN_USER,
    ORIGIN,
    build_config,
    build_control,
    env_file,
    fake_docker,
)


@pytest.fixture
def volume(tmp_path):
    """A game volume with the two directories the manager writes into."""
    (tmp_path / "worlds_local").mkdir()
    (tmp_path / "backups").mkdir()
    return tmp_path


@pytest.fixture
def world_store(volume):
    return WorldStore(volume / "worlds_local", backups_dir=volume / "backups")


@pytest.fixture
def backup_store(world_store):
    return BackupStore(world_store)


def make_world(store: WorldStore, name: str, body: bytes = b"world-data") -> None:
    """A 1.0 world on the volume: a directory holding `_main.1.db2` and `.fwl2`."""
    directory = store.root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "_main.1.db2").write_bytes(body)
    (directory / "_main.1.fwl2").write_bytes(b"fwl")


# --------------------------------------------------- the world in the filename


@pytest.mark.parametrize("world", ["Base-2", "Fjord-2024", "Server-1-2"])
def test_a_world_whose_name_ends_in_a_number_is_read_back_from_its_archive(
    world_store, backup_store, world
):
    """`Base-2` must not come back as `Base`.

    The stamp is a known fixed shape, so only the stamp may be peeled off. Peeling
    every trailing number takes the world's own suffix with it.
    """
    make_world(world_store, world)
    made = world_store.backup(world)

    entry = backup_store.find(made.name)

    assert entry is not None
    assert entry.world == world


def test_a_world_named_only_digits_is_still_read_back_whole(world_store, backup_store):
    """The guard that already exists, kept so the fix above cannot undo it."""
    make_world(world_store, "2024")
    made = world_store.backup("2024")

    entry = backup_store.find(made.name)

    assert entry is not None
    assert entry.world == "2024"


def test_the_duplicate_second_counter_is_not_mistaken_for_the_world(
    world_store, backup_store
):
    """Two backups inside one second get a `-2` suffix, which is not part of the name."""
    make_world(world_store, "Old-World")
    names = [world_store.backup("Old-World", prefix=SCHEDULED_PREFIX).name for _ in range(3)]

    worlds = {backup_store.find(name).world for name in names}

    assert worlds == {"Old-World"}


# ------------------------------------------------------------------ retention


def test_retention_prunes_a_world_whose_name_ends_in_a_number(world_store, backup_store):
    """Nothing else ever deletes a SCHEDULED- archive, so a world that retention
    cannot recognise fills the volume -- and the game server is what breaks when it
    does."""
    make_world(world_store, "Base-2")
    for _ in range(5):
        world_store.backup("Base-2", prefix=SCHEDULED_PREFIX)
        time.sleep(0.01)

    removed = backup_store.prune("Base-2", keep=2)

    assert len(removed) == 3
    assert len(backup_store.entries()) == 2


def test_retention_leaves_manual_and_game_archives_alone(world_store, backup_store):
    make_world(world_store, "Midgard")
    for _ in range(3):
        world_store.backup("Midgard", prefix=SCHEDULED_PREFIX)
        time.sleep(0.01)
    manual = world_store.backup("Midgard").name
    game = backup_store.root / "worlds-20260101-1200.zip"
    game.write_bytes(b"PK\x05\x06" + b"\0" * 18)

    backup_store.prune("Midgard", keep=1)

    survivors = {entry.name for entry in backup_store.entries()}
    assert manual in survivors
    assert game.name in survivors


# -------------------------------------------------------------------- restore


def test_restoring_a_world_whose_name_ends_in_a_number_lands_under_that_name(
    world_store, backup_store
):
    """Restoring `Base-2` must not quietly produce a world called `Base`, which the
    server -- still set to load `Base-2` -- would not find."""
    make_world(world_store, "Base-2")
    made = world_store.backup("Base-2")
    for child in (world_store.root / "Base-2").iterdir():
        child.unlink()
    (world_store.root / "Base-2").rmdir()

    placed = backup_store.restore(made.name)

    assert placed.name == "Base-2"
    assert (world_store.root / "Base-2" / "_main.1.db2").is_file()


# ------------------------------------------------- archives of long-named worlds


@pytest.mark.parametrize(
    "prefix, length",
    [("", 64), (SCHEDULED_PREFIX, 64)],
    ids=["manual", "scheduled"],
)
def test_a_backup_of_a_long_named_world_can_be_deleted(
    world_store, backup_store, prefix, length
):
    """The world-name cap is 64, and the archive name is always longer than the world
    it holds -- so validating the archive with the world's rule refuses names the
    manager itself just wrote."""
    world = "W" * length
    make_world(world_store, world)
    made = world_store.backup(world, prefix=prefix) if prefix else world_store.backup(world)

    gone = backup_store.delete(made.name)

    assert gone.name == made.name
    assert not (backup_store.root / made.name).exists()


def test_a_backup_of_a_long_named_world_can_be_restored(world_store, backup_store):
    world = "W" * 64
    make_world(world_store, world)
    made = world_store.backup(world)

    placed = backup_store.restore(made.name, target_name="Restored")

    assert placed.name == "Restored"


def test_an_archive_name_that_escapes_the_folder_is_still_refused(backup_store):
    """The cap may be wrong for archives; the traversal rules are not."""
    for bad in ("../secrets.zip", "sub/dir.zip", "..\\escape.zip"):
        with pytest.raises(BackupError):
            backup_store.find(bad)


def test_a_refused_archive_name_is_a_backup_error_not_a_world_error(backup_store):
    """The routes catch BackupError. A WorldError escaping this far is a 500."""
    with pytest.raises(BackupError):
        backup_store.find("W" * 200 + ".zip")


# ------------------------------------------------------------ through the app


@pytest.fixture
def backups_client(volume, env_file, fake_docker):
    """The real app, pointed at the temp volume, with the backup timer left off.

    The state directory exists, because saving a schedule into a missing one is
    refused -- that guard has its own test above. The schedule is seeded *disabled*
    instead, so the timer starts and then finds nothing owed: an enabled one is due
    immediately on a first run and would back up a world in the background of every
    test here.
    """
    state = volume / "state"
    state.mkdir()
    (state / "backup-schedule.json").write_text(
        json.dumps({"enabled": False}), encoding="utf-8"
    )
    config = build_config(
        env_file,
        worlds_dir=str(volume / "worlds_local"),
        backups_dir=str(volume / "backups"),
        backup_schedule_file=str(state / "backup-schedule.json"),
    )
    control = build_control(config, fake_docker)
    app = create_app(config=config, controller=control, settings=SettingsStore(env_file))
    with TestClient(app) as client:
        response = client.post(
            "/login",
            data={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        yield client


def test_deleting_a_backup_of_a_long_named_world_is_not_a_server_error(
    backups_client, world_store
):
    """The panel renders the row as deletable, so pressing Delete must not 500."""
    world = "W" * 64
    make_world(world_store, world)
    made = world_store.backup(world)

    response = backups_client.post(
        "/api/backups/delete", json={"name": made.name}, headers={"Origin": ORIGIN}
    )

    assert response.status_code == 200, response.text
    assert not (world_store.backups_dir / made.name).exists()


def test_every_listed_backup_that_says_it_is_deletable_can_be_deleted(
    backups_client, world_store
):
    """Whatever the rules are, the row and the endpoint have to agree on them."""
    make_world(world_store, "W" * 64)
    make_world(world_store, "Base-2")
    world_store.backup("W" * 64)
    world_store.backup("Base-2", prefix=SCHEDULED_PREFIX)

    listed = backups_client.get("/api/backups").json()["backups"]
    deletable = [row["name"] for row in listed if row["deletable"]]
    assert deletable, "nothing was listed as deletable, so this proves nothing"

    for name in deletable:
        response = backups_client.post(
            "/api/backups/delete", json={"name": name}, headers={"Origin": ORIGIN}
        )
        assert response.status_code == 200, f"{name}: {response.text}"


# ------------------------------------------------- backing up at a set time


def at(hour: int, minute: int, *, days: int = 0) -> float:
    """A local wall-clock time today (or `days` away), as an epoch.

    Built through ``datetime`` rather than by adding 86400s, because the whole point
    of the mode is that it tracks the clock on the wall through a DST change.
    """
    moment = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    return (moment + timedelta(days=days)).timestamp()


def test_a_daily_schedule_is_due_at_that_time_the_next_day():
    schedule = BackupSchedule(
        mode=MODE_DAILY, daily_time="13:30", last_run_at=at(13, 30, days=-1)
    )

    assert schedule.due_at() == pytest.approx(at(13, 30), abs=1)


def test_a_daily_schedule_run_before_its_time_is_due_later_the_same_day():
    schedule = BackupSchedule(mode=MODE_DAILY, daily_time="13:30", last_run_at=at(12, 0))

    assert schedule.due_at() == pytest.approx(at(13, 30), abs=1)


def test_a_daily_schedule_run_after_its_time_waits_for_tomorrow():
    """Firing again the same day would mean two backups from one 'every day at'."""
    schedule = BackupSchedule(mode=MODE_DAILY, daily_time="13:30", last_run_at=at(14, 0))

    assert schedule.due_at() == pytest.approx(at(13, 30, days=1), abs=1)


def test_a_daily_time_just_after_midnight_is_due_minutes_later_not_a_day():
    """`00:30` after a run at `23:59` is half an hour away. Adding 24h to the last
    run -- the obvious arithmetic -- would put it a whole day out."""
    schedule = BackupSchedule(mode=MODE_DAILY, daily_time="00:30", last_run_at=at(23, 59))

    assert schedule.due_at() == pytest.approx(at(0, 30, days=1), abs=1)


def test_a_daily_schedule_missed_while_the_manager_was_down_is_due_now():
    """The answer to 'what if the server was off at 13:30': take it late rather than
    skip the day. One backup, not one per restart -- `last_run_at` advances."""
    schedule = BackupSchedule(
        mode=MODE_DAILY, daily_time="13:30", last_run_at=at(13, 30, days=-3)
    )

    assert schedule.due_at() < time.time()


def test_a_daily_schedule_that_has_never_run_is_due_immediately():
    schedule = BackupSchedule(mode=MODE_DAILY, daily_time="13:30", last_run_at=None)

    assert schedule.due_at() == 0.0


def test_a_disabled_daily_schedule_is_never_due():
    schedule = BackupSchedule(
        mode=MODE_DAILY, daily_time="13:30", enabled=False, last_run_at=at(13, 30)
    )

    assert schedule.due_at() is None


def test_an_interval_schedule_is_unchanged_by_the_new_mode():
    ran = at(12, 0)
    schedule = BackupSchedule(interval_hours=6, last_run_at=ran)

    assert schedule.mode == MODE_INTERVAL
    assert schedule.due_at() == pytest.approx(ran + 6 * 3600, abs=1)


# ------------------------------------------------------ what the panel posts


@pytest.mark.parametrize(
    "posted, stored",
    [("13:30", "13:30"), ("1:30", "01:30"), ("00:00", "00:00"), ("9:05", "09:05")],
)
def test_a_time_of_day_is_stored_in_a_single_shape(posted, stored):
    """`1:30` and `01:30` are the same time; storing both spellings would make the
    panel show back something other than what it will do."""
    result = schedule_from_payload(
        {"mode": MODE_DAILY, "daily_time": posted}, BackupSchedule()
    )

    assert result.daily_time == stored


@pytest.mark.parametrize("posted", ["25:00", "12:60", "half twelve", "", "1330", None])
def test_an_unreadable_time_leaves_the_stored_one_alone(posted):
    """Same shape as the number fields: the panel cannot fail a save, and what it
    shows afterwards is what was actually kept."""
    current = BackupSchedule(mode=MODE_DAILY, daily_time="13:30")

    result = schedule_from_payload({"mode": MODE_DAILY, "daily_time": posted}, current)

    assert result.daily_time == "13:30"


def test_an_unknown_mode_leaves_the_stored_one_alone():
    current = BackupSchedule(mode=MODE_DAILY, daily_time="13:30")

    result = schedule_from_payload({"mode": "whenever"}, current)

    assert result.mode == MODE_DAILY


def test_choosing_an_interval_keeps_the_time_that_was_set_before():
    """Switching modes must not throw the other mode's setting away -- switching back
    should find it where it was left."""
    current = BackupSchedule(mode=MODE_DAILY, daily_time="13:30")

    result = schedule_from_payload({"mode": MODE_INTERVAL, "interval_hours": 6}, current)

    assert result.mode == MODE_INTERVAL
    assert result.daily_time == "13:30"


# ----------------------------------------------------------- on disk


def test_the_mode_and_time_survive_a_restart(tmp_path):
    store = ScheduleStore(tmp_path / "backup-schedule.json")
    store.save(BackupSchedule(mode=MODE_DAILY, daily_time="01:30"))

    reloaded = ScheduleStore(tmp_path / "backup-schedule.json").load()

    assert reloaded.mode == MODE_DAILY
    assert reloaded.daily_time == "01:30"


def test_a_schedule_file_written_before_this_feature_still_reads(tmp_path):
    """No migration: a file with neither key is an interval schedule, as it was."""
    path = tmp_path / "backup-schedule.json"
    path.write_text(
        json.dumps({"enabled": True, "interval_hours": 12, "keep_per_world": 3}),
        encoding="utf-8",
    )

    loaded = ScheduleStore(path).load()

    assert loaded.mode == MODE_INTERVAL
    assert loaded.interval_hours == 12
    assert loaded.due_at() == 0.0


def test_a_stored_time_that_is_not_a_time_falls_back_to_the_default(tmp_path):
    path = tmp_path / "backup-schedule.json"
    path.write_text(json.dumps({"mode": "daily", "daily_time": "banana"}), encoding="utf-8")

    loaded = ScheduleStore(path).load()

    assert loaded.daily_time == BackupSchedule().daily_time


# --------------------------------------------------------- through the app


def test_the_panel_can_set_a_time_of_day(backups_client):
    response = backups_client.post(
        "/api/backups/schedule",
        json={"enabled": True, "mode": MODE_DAILY, "daily_time": "1:30", "keep_per_world": 5},
        headers={"Origin": ORIGIN},
    )

    assert response.status_code == 200, response.text
    schedule = response.json()["schedule"]
    assert schedule["mode"] == MODE_DAILY
    assert schedule["daily_time"] == "01:30"


def test_the_panel_is_told_the_default_time_rather_than_hardcoding_one(backups_client):
    """Every other bound in the card is rendered from the value the manager enforces."""
    page = backups_client.get("/").text

    assert "backup-daily-time" in page


def test_the_time_field_sits_inside_the_schedule_form(backups_client):
    """app.js binds one submit handler per form; a control outside its form is inert.
    This is the bug that left `Add mod` doing nothing while 506 tests passed."""
    page = backups_client.get("/").text
    form_start = page.index('id="backup-schedule-form"')
    form_end = page.index("</form>", form_start)

    assert form_start < page.index('id="backup-daily-time"') < form_end
