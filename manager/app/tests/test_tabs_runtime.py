"""The dashboard's tab strip, executed rather than read.

Everything else in this suite checks the *rendered markup* and the *text* of
``app.js``. That is enough for what the server decides -- which tab the page opens
on before any script runs, that every panel is rendered rather than built on demand
-- and it is worth nothing for the rest, because the tab strip is almost entirely
runtime: which panel is showing, whether the console kept its buffer, whether a lock
reached the strip, whether following resumes when the console comes back.

A source-text assertion cannot tell those apart from a file that merely contains the
right words. ``manager/app/tests/js/tabs.mjs`` runs the real ``app.js`` against the
real rendered page in jsdom and reports one result per promise; this module renders
the page through the app, runs it, and turns each result into a test.

Setup (once)::

    cd manager/app/tests/js && npm install

Deliberately NOT skipped when node is missing: a skipped test protects nothing, and
these are the only checks standing behind the tab strip's behaviour.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# `stack` builds the app on a temp env file with a fake Docker; pytest resolves a
# fixture's own dependencies by name, so env_file and fake_docker have to come
# across too or `stack` cannot be built here.
from app.tests.test_edge_cases import (  # noqa: F401  (fixtures)
    env_file,
    fake_docker,
    login,
    stack,
)

JS_DIR = Path(__file__).resolve().parent / "js"
HARNESS = JS_DIR / "tabs.mjs"
STATIC = Path(__file__).resolve().parents[1] / "static"

# Every promise the harness reports on. Listed here as well so that a case silently
# disappearing from the harness fails the suite rather than quietly reducing it.
CASES = [
    "lands_on_console_with_no_stored_tab",
    "the_console_is_never_rebuilt_across_a_switch",
    "returning_to_the_console_resumes_following",
    "returning_to_the_console_leaves_a_parked_view_alone",
    "a_switch_issues_no_request_and_leaves_the_socket_alone",
    "running_leaves_both_tabs_openable_but_locked",
    "stopping_unlocks_both_tabs_without_a_tab_switch",
    "a_stored_tab_comes_back_on_load",
    "an_unusable_stored_tab_falls_back_to_console",
    "a_switch_is_remembered",
    "blocked_storage_still_leaves_a_usable_dashboard",
    "the_keyboard_walks_the_strip",
    "exactly_one_panel_is_ever_visible",
    "the_settings_table_shows_plain_names",
    "every_panel_can_take_focus",
    "a_panel_error_survives_a_switch_and_is_marked_in_the_strip",
    "an_upload_in_flight_is_marked_in_the_strip",
    "a_tab_pointing_at_a_missing_panel_does_not_kill_the_dashboard",
]

SETUP = (
    "The dashboard's runtime tests need node and jsdom:\n"
    "    cd manager/app/tests/js && npm install\n"
    "They are not skipped when the toolchain is missing -- a skipped test protects\n"
    "nothing, and these are the only checks behind the tab strip's behaviour."
)


# The harness builds ~20 jsdom pages, so it runs once for the whole module and every
# case reads the same report. A module-scoped fixture cannot be used: `stack` (the
# app, its fake Docker and its temp env file) is function-scoped by design.
_REPORT: dict[str, dict] = {}


@pytest.fixture
def tab_results(stack, tmp_path_factory) -> dict[str, dict]:  # noqa: F811
    if _REPORT:
        return _REPORT
    node = shutil.which("node")
    if node is None:
        pytest.fail(f"node was not found on PATH.\n{SETUP}")
    if not (JS_DIR / "node_modules" / "jsdom").is_dir():
        pytest.fail(f"jsdom is not installed.\n{SETUP}")

    # The page as the manager actually renders it, not a fixture copy that can drift.
    with TestClient(stack["app"]) as client:
        login(client)
        page = client.get("/").text
    rendered = tmp_path_factory.mktemp("dashboard") / "index.html"
    rendered.write_text(page, encoding="utf-8")

    done = subprocess.run(
        [node, str(HARNESS), str(rendered), str(STATIC / "app.js"), str(STATIC / "style.css")],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=JS_DIR,
    )
    if done.returncode != 0:
        pytest.fail(f"the jsdom harness crashed (exit {done.returncode})\n{done.stderr.strip()}")
    try:
        _REPORT.update(json.loads(done.stdout))
    except json.JSONDecodeError:  # pragma: no cover - only on a harness bug
        pytest.fail(f"the harness printed no JSON:\n{done.stdout[-2000:]}\n{done.stderr[-2000:]}")
    return _REPORT


@pytest.mark.parametrize("case", CASES)
def test_tab_strip_behaviour(tab_results, case):
    assert case in tab_results, (
        f"the harness reported no result for {case!r} -- was the case renamed or removed? "
        f"It reported: {sorted(tab_results)}"
    )
    result = tab_results[case]
    assert result["ok"], result["detail"]


def test_no_case_was_added_to_the_harness_without_being_listed(tab_results):
    """The parametrize list above is the contract; a case only in the harness would
    otherwise run without any test naming it."""
    assert sorted(tab_results) == sorted(CASES)
