"""Server settings picks between worlds that exist; it never makes one.

The World field used to be free text, and a name matching no save had Valheim generate
a brand-new world on the next Start -- which read as "renaming the world did nothing
and made a new one". It is now a list of the worlds on the server, and the manager
refuses a name with no world behind it, so a hand-made request cannot do it either.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.settings_store import SettingsStore
from app.tests.test_edge_cases import (  # noqa: F401  (fixtures)
    ORIGIN,
    env_file,
    fake_docker,
    login,
    make_modern_world,
    save_settings,
    worlds,
    worlds_dir,
)


def test_the_world_field_is_a_list_not_a_text_box(worlds):
    with TestClient(worlds["app"]) as client:
        login(client)
        html = client.get("/").text

    assert '<select id="field-WORLD_NAME" name="WORLD_NAME"' in html
    assert 'id="field-WORLD_NAME" name="WORLD_NAME" type="text"' not in html


def test_picking_an_existing_world_saves_it(worlds):
    make_modern_world(worlds["dir"], "Dedicated")
    make_modern_world(worlds["dir"], "Midgard")
    worlds["docker"].seed_stopped()

    with TestClient(worlds["app"]) as client:
        login(client)
        response = save_settings(client, {"WORLD_NAME": "Midgard"})

    assert response.status_code == 200, response.text
    assert response.json()["changed"] == ["WORLD_NAME"]
    assert SettingsStore(worlds["env"]).read()["WORLD_NAME"] == "Midgard"


def test_a_world_name_with_no_world_is_refused_and_nothing_is_written(worlds):
    make_modern_world(worlds["dir"], "Dedicated")
    container = worlds["docker"].seed_stopped()
    before = worlds["env"].read_text(encoding="utf-8")

    with TestClient(worlds["app"]) as client:
        login(client)
        response = save_settings(client, {"WORLD_NAME": "Brandnew"})

    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert '"Brandnew"' in error and "Worlds tab" in error
    assert worlds["env"].read_text(encoding="utf-8") == before
    assert container.removed is False


def test_the_match_is_exact_because_valheim_looks_the_save_up_by_file_name(worlds):
    make_modern_world(worlds["dir"], "Dedicated")
    make_modern_world(worlds["dir"], "Midgard")
    worlds["docker"].seed_stopped()

    with TestClient(worlds["app"]) as client:
        login(client)
        response = save_settings(client, {"WORLD_NAME": "midgard"})

    assert response.status_code == 400, response.text


def test_an_unchanged_world_name_is_not_checked(worlds):
    """A fresh install names a world the first Start creates. Saving something else
    must not be blocked by that world not existing yet."""
    worlds["docker"].seed_stopped()  # no worlds on disk at all

    with TestClient(worlds["app"]) as client:
        login(client)
        current = SettingsStore(worlds["env"]).read()["WORLD_NAME"]
        response = save_settings(client, {"WORLD_NAME": current, "SERVER_NAME": "Asgard"})

    assert response.status_code == 200, response.text
    assert response.json()["changed"] == ["SERVER_NAME"]
