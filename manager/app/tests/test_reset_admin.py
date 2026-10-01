"""tools/reset_admin.py: a forgotten login, reset in place.

It must change the admin username and password and nothing else, refuse when its
write would be ignored (credentials from manager.env) or meaningless (no admin yet),
and leave a credential file the manager itself accepts.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from app.auth import hash_password, verify_password
from app.main import AppConfig, _resolve_credentials
from app.state_store import ManagerState, StateStore

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import reset_admin  # noqa: E402

OLD_PASSWORD = "old-password-123"
NEW_PASSWORD = "brand-new-pass-456"


@pytest.fixture
def state(tmp_path, monkeypatch):
    for name in ("ADMIN_USER", "ADMIN_PASSWORD_HASH", "SESSION_SECRET", "MANAGER_STATE_FILE"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "manager-state.json"
    StateStore(path).save(
        ManagerState(
            admin_user="odin",
            admin_password_hash=hash_password(OLD_PASSWORD, rounds=4),
            session_secret="s" * 48,
        )
    )
    # Neighbours on the same volume, which a reset must not touch.
    (tmp_path / "backup-schedule.json").write_text('{"enabled": true}', encoding="utf-8")
    (tmp_path / "players.json").write_text('{"x": 1}', encoding="utf-8")
    return path


def run(state_path, monkeypatch, *args, password=NEW_PASSWORD):
    monkeypatch.setattr(sys, "stdin", io.StringIO(password + "\n"))
    return reset_admin.main(["--stdin", "--state-file", str(state_path), *args])


def test_it_sets_a_new_password_and_signs_everyone_out(state, monkeypatch):
    created = json.loads(state.read_text(encoding="utf-8"))["created_at"]

    assert run(state, monkeypatch) == 0

    saved = StateStore(state).load()
    assert saved.admin_user == "odin"  # --stdin without --user keeps the name
    assert verify_password(NEW_PASSWORD, saved.admin_password_hash)
    assert not verify_password(OLD_PASSWORD, saved.admin_password_hash)
    assert saved.session_secret != "s" * 48
    assert json.loads(state.read_text(encoding="utf-8"))["created_at"] == created
    assert (state.stat().st_mode & 0o777) == 0o600 or sys.platform == "win32"


def test_it_can_change_the_username_too(state, monkeypatch):
    assert run(state, monkeypatch, "--user", "thor") == 0
    assert StateStore(state).load().admin_user == "thor"


def test_keep_sessions_keeps_the_secret(state, monkeypatch):
    assert run(state, monkeypatch, "--keep-sessions") == 0
    assert StateStore(state).load().session_secret == "s" * 48


def test_nothing_else_on_the_volume_is_touched(state, monkeypatch):
    assert run(state, monkeypatch) == 0
    folder = state.parent
    assert (folder / "backup-schedule.json").read_text(encoding="utf-8") == '{"enabled": true}'
    assert (folder / "players.json").read_text(encoding="utf-8") == '{"x": 1}'


def test_the_manager_accepts_the_new_login(state, monkeypatch):
    assert run(state, monkeypatch, "--user", "thor") == 0
    auth, source = _resolve_credentials(AppConfig(state_file=str(state)), StateStore(state))
    assert source == "state file"
    assert auth is not None


@pytest.mark.parametrize(
    "args,password",
    [
        ((), "short"),  # the wizard's 10-character minimum applies here too
        (("--user", "two words"), NEW_PASSWORD),
        (("--user", "   "), NEW_PASSWORD),
    ],
)
def test_the_wizards_rules_apply_and_a_refusal_writes_nothing(state, monkeypatch, args, password):
    before = state.read_bytes()
    assert run(state, monkeypatch, *args, password=password) == 2
    assert state.read_bytes() == before


def test_a_login_from_manager_env_is_refused_rather_than_silently_ignored(state, monkeypatch):
    monkeypatch.setenv("ADMIN_USER", "odin")
    before = state.read_bytes()
    assert run(state, monkeypatch) == 2
    assert state.read_bytes() == before


def test_with_no_admin_yet_it_points_at_setup_and_creates_nothing(tmp_path, monkeypatch):
    for name in ("ADMIN_USER", "ADMIN_PASSWORD_HASH", "SESSION_SECRET"):
        monkeypatch.delenv(name, raising=False)
    missing = tmp_path / "manager-state.json"
    assert run(missing, monkeypatch) == 2
    assert not missing.exists()
