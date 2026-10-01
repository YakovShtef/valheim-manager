"""Server settings shows one backup schedule, read-only: the one set on the Worlds tab.

The Settings tab used to show the game image's own BACKUPS* keys in an "Updates &
backups" card that could not be edited, beside a different, editable schedule on the
Worlds tab. Now it shows the Worlds schedule's values, and the Worlds tab is the only
place to change them.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.tests.test_edge_cases import (  # noqa: F401  (fixtures)
    env_file,
    fake_docker,
    login,
    stack,
)


def _panels(client: TestClient) -> tuple[str, str]:
    html = client.get("/").text
    settings = html[html.index('id="panel-settings"'):html.index('id="panel-worlds"')]
    worlds = html[html.index('id="panel-worlds"'):html.index('id="panel-mods"')]
    return settings, worlds


def test_settings_shows_the_backup_schedule_read_only(stack):
    with TestClient(stack["app"]) as client:
        login(client)
        settings, worlds = _panels(client)

    assert 'id="settings-backup-card"' in settings
    for value in ("settings-backup-auto", "settings-backup-when", "settings-backup-keep",
                  "settings-backup-last"):
        assert f'id="{value}"' in settings
    # Nothing in Settings can change a backup setting: no schedule form, no fields.
    assert "backup-schedule-form" not in settings
    assert "field-BACKUPS" not in settings
    # The schedule is edited on the Worlds tab, and only there.
    assert 'id="backup-schedule-form"' in worlds
    assert 'data-goto-tab="worlds"' in settings


def test_the_game_images_own_backup_keys_are_not_shown(stack, env_file):
    """One schedule on screen: the image's BACKUPS* keys are left in the file, unshown."""
    with env_file.open("a", encoding="utf-8") as handle:
        handle.write("BACKUPS=true\nBACKUPS_INTERVAL=3600\nBACKUPS_MAX_AGE=3\n")
    with TestClient(stack["app"]) as client:
        login(client)
        settings, _ = _panels(client)
        rows = client.get("/api/status").json()["settings"]

    assert any(row["key"] == "BACKUPS" for row in rows)  # still in the file
    assert "BACKUPS_INTERVAL" not in settings
    assert "BACKUPS_MAX_AGE" not in settings
    assert "Updates &amp; backups" not in settings


def test_the_settings_panel_cannot_write_the_game_backup_keys(stack, env_file):
    stack["docker"].seed_stopped()
    before = env_file.read_text(encoding="utf-8")
    with TestClient(stack["app"]) as client:
        login(client)
        response = client.post(
            "/api/settings",
            json={"settings": {"BACKUPS": "false", "BACKUPS_MAX_AGE": "1"}},
            headers={"Origin": "http://testserver"},
        )

    assert response.status_code == 200, response.text
    assert env_file.read_text(encoding="utf-8") == before
