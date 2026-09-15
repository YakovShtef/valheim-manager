"""Edge cases from the spec's I/O matrix, exercised against a fake Docker engine.

No Docker daemon is needed: ``FakeDockerClient`` implements exactly the slice of the
SDK that ``docker_control`` uses, so every path (including the failure paths that are
hard to provoke for real) is covered.
"""

from __future__ import annotations

import io
import itertools
import json
import logging
import os
import re
import stat
import time
import zipfile
from pathlib import Path

import pytest
import requests
from docker.errors import APIError, ImageNotFound, NotFound
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.auth import (
    AuthConfigError,
    PasswordTooLongError,
    SessionAuth,
    hash_password,
    validate_password_hash,
    verify_password,
)
from app.docker_control import (
    PHASE_CREATING,
    PHASE_PULLING,
    PHASE_STARTING,
    PHASE_STOPPING,
    DockerControl,
    DockerControlError,
)
from app.main import AppConfig, ConfigError, create_app
from app.settings_store import (
    DEFAULT_SETTINGS_TEXT,
    MASK,
    SettingsFileError,
    SettingsStore,
    parse_env_text,
    render_env_text,
)
from app.modifiers import (
    CATEGORIES,
    PRESETS,
    TOGGLES,
    ModifierError,
    Modifiers,
    compose,
    fields_to_modifiers,
    parse,
)
from app.setup import (
    MIN_SERVER_PASS_LENGTH,
    SetupInputError,
    validated_modifiers,
    validated_settings,
)
from app.worlds import (
    LAYOUT_LEGACY,
    LAYOUT_MODERN,
    WORLD_DIR_MODE,
    WORLD_FILE_MODE,
    WorldError,
    WorldStore,
    human_size,
    sanitised_name,
)
from app.state_store import (
    STATE_MODE,
    ManagerState,
    StateStore,
    StateStoreError,
    new_session_secret,
)

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix", reason="file modes are only meaningful on POSIX"
)

IMAGE = "ghcr.io/community-valheim-tools/valheim-server:latest"
CONTAINER = "valheim-server"
ADMIN_USER = "odin"
ADMIN_PASSWORD = "correct-horse-battery"
# rounds=4: the lowest bcrypt cost, so the suite stays fast.
ADMIN_HASH = hash_password(ADMIN_PASSWORD, algo="bcrypt", rounds=4)
SESSION_SECRET = "a-stable-secret-for-tests-0123456789"
ORIGIN = "http://testserver"

READY_LINE = "09/12/2026 18:04:11: Game server connected"

ENV_FILE_TEXT = """\
# Valheim settings
SERVER_NAME="Midgard Test"
WORLD_NAME=Dedicated
SERVER_PORT=2456
SERVER_PASS='hunter22'
export SERVER_PUBLIC=1
CROSSPLAY=false
TZ=Etc/UTC   # inline comment
"""


# --------------------------------------------------------------------- fakes


class FakeImages:
    def __init__(self, present: set[str], client: "FakeDockerClient | None" = None):
        self.client = client
        self.present = set(present)
        self.pulls: list[tuple[str, str]] = []
        self.pull_error: Exception | None = None

    def get(self, name: str):
        if name not in self.present:
            raise ImageNotFound(f"no such image: {name}")
        return {"Id": "sha256:deadbeef"}

    def pull(self, repository: str, tag: str | None = None):
        if self.client is not None and self.client.pull_hook is not None:
            self.client.pull_hook()
        if self.pull_error is not None:
            raise self.pull_error
        self.pulls.append((repository, tag or "latest"))
        self.present.add(f"{repository}:{tag or 'latest'}")
        return {"Id": "sha256:deadbeef"}


# Log lines get one synthetic second each, and a run's StartedAt is the stamp of its
# first line -- so `since=StartedAt` genuinely separates runs, as it does on a real
# engine. Without this the fake replays a previous run's ready line after a restart.
LOG_EPOCH_BASE = 1_789_000_000


def _iso(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + ".000000000Z"


class FakeContainer:
    _ids = itertools.count(1)

    def __init__(self, client: "FakeDockerClient", name: str, create_kwargs: dict):
        self.client = client
        self.name = name
        self.id = f"fake{next(self._ids):04d}"
        self.create_kwargs = create_kwargs
        self.status = "created"
        self.started_at = "0001-01-01T00:00:00Z"
        self.start_count = 0
        # Index into log_messages at which the current run began.
        self.run_log_offset = 0
        self.exit_code = 0
        self.log_messages: list[str] = []
        # Seconds between synthetic log lines. 0 puts a whole burst inside one
        # second, which is what SteamCMD's first-run progress output really does and
        # what `since`'s whole-second resolution has to cope with.
        self.log_epoch_step = 1
        self.removed = False
        self.kill_calls = 0
        self.stop_calls: list[int | None] = []
        self.logs_calls: list[dict] = []
        self.reload_error: Exception | None = None

    # --- the SDK surface docker_control touches -------------------------

    @property
    def attrs(self) -> dict:
        return {
            "State": {
                "Status": self.status,
                "StartedAt": self.started_at,
                "ExitCode": self.exit_code,
            }
        }

    def reload(self) -> None:
        if self.reload_error is not None:
            raise self.reload_error

    def start(self) -> None:
        if self.client.start_hook is not None:
            self.client.start_hook()
        if self.client.start_error is not None:
            raise self.client.start_error
        self.status = "running"
        self.start_count += 1
        # Each run gets its own StartedAt, like the real engine: anything already in
        # the log belongs to the previous run.
        self.run_log_offset = len(self.log_messages)
        self.started_at = _iso(LOG_EPOCH_BASE + self.run_log_offset)

    def stop(self, timeout: int | None = None) -> None:
        self.stop_calls.append(timeout)
        if self.client.stop_error is not None:
            raise self.client.stop_error
        self.status = "exited"

    def kill(self) -> None:
        self.kill_calls += 1
        self.status = "exited"

    def remove(self, force: bool = False) -> None:
        self.removed = True
        self.client.containers.registry.pop(self.name, None)

    def logs(self, **kwargs) -> bytes:
        self.logs_calls.append(kwargs)
        if self.client.logs_error is not None:
            raise self.client.logs_error
        records = [
            (LOG_EPOCH_BASE + int(index * self.log_epoch_step), message)
            for index, message in enumerate(self.log_messages)
        ]
        since = kwargs.get("since")
        if since:
            records = [r for r in records if r[0] >= since]
        tail = kwargs.get("tail")
        if isinstance(tail, int):
            # Docker applies the line cap after the `since` window, as we do here.
            records = records[-tail:] if tail else []
        out = [f"{_iso(epoch)} {message}" for epoch, message in records]
        return (chr(10).join(out) + chr(10)).encode("utf-8") if out else b""


class FakeContainers:
    def __init__(self, client: "FakeDockerClient"):
        self.client = client
        self.registry: dict[str, FakeContainer] = {}
        self.create_calls: list[dict] = []

    def get(self, name: str) -> FakeContainer:
        if self.client.get_error is not None:
            raise self.client.get_error
        if name not in self.registry:
            raise NotFound(f"no such container: {name}")
        return self.registry[name]

    def create(self, image, **kwargs) -> FakeContainer:
        if self.client.create_hook is not None:
            self.client.create_hook()
        if self.client.create_error is not None:
            raise self.client.create_error
        name = kwargs["name"]
        self.create_calls.append({"image": image, **kwargs})
        container = FakeContainer(self.client, name, {"image": image, **kwargs})
        self.registry[name] = container
        return container


class FakeDockerClient:
    def __init__(self, *, images: set[str] | None = None):
        self.images = FakeImages(images if images is not None else set(), client=self)
        self.containers = FakeContainers(self)
        self.create_error: Exception | None = None
        self.start_error: Exception | None = None
        self.stop_error: Exception | None = None
        self.logs_error: Exception | None = None
        self.get_error: Exception | None = None
        # Called *inside* the corresponding engine call, so a test can observe the
        # transient phase the UI is supposed to show while the work is in flight.
        self.pull_hook = None
        self.create_hook = None
        self.start_hook = None
        self.closed = False

    def close(self) -> None:
        self.closed = True

    # --- test helpers ---------------------------------------------------

    def seed_running(self, *, log_messages: list[str] | None = None) -> FakeContainer:
        container = self.containers.create(IMAGE, name=CONTAINER)
        container.status = "running"
        container.log_messages = list(log_messages or [])
        # This run owns every seeded line.
        container.run_log_offset = 0
        container.started_at = _iso(LOG_EPOCH_BASE)
        return container

    def seed_stopped(self) -> FakeContainer:
        container = self.containers.create(IMAGE, name=CONTAINER)
        container.status = "exited"
        return container


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def project_root():
    """Repo root: manager/app/tests -> manager/app -> manager -> root.

    Only the ``./manager`` directory is in the image's build context, so inside the
    container this resolves to ``/`` and the repo-root files these tests check are
    simply not there. Skip rather than fail: the answer is to run the suite from a
    checkout (``pip install -r manager/requirements-dev.txt``), which is what the
    README now says.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    if not (root / "valheim.env.example").is_file():
        pytest.skip(
            "repo-root files are outside the image build context; run the suite from "
            "a checkout, not inside the manager container"
        )
    return root


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / "valheim.env"
    path.write_text(ENV_FILE_TEXT, encoding="utf-8")
    return path


@pytest.fixture
def fake_docker():
    return FakeDockerClient()


def build_config(env_file, **overrides) -> AppConfig:
    base = dict(
        container_name=CONTAINER,
        image=IMAGE,
        network="test-net",
        config_volume="valheim-config",
        data_volume="valheim-data",
        env_file=str(env_file),
        admin_user=ADMIN_USER,
        admin_password_hash=ADMIN_HASH,
        session_secret=SESSION_SECRET,
        log_poll_seconds=0.01,
        status_every_n_polls=1,
        stop_timeout=7,
    )
    base.update(overrides)
    return AppConfig(**base)


def build_control(config: AppConfig, fake_docker: FakeDockerClient) -> DockerControl:
    store = SettingsStore(config.env_file)
    return DockerControl(
        base_url=config.docker_host,
        container_name=config.container_name,
        image=config.image,
        network=config.network,
        config_volume=config.config_volume,
        data_volume=config.data_volume,
        env_provider=store.container_env,
        port_provider=lambda: [store.server_port(), store.server_port() + 1, store.server_port() + 2],
        ready_pattern=config.ready_pattern,
        stop_timeout=config.stop_timeout,
        restart_policy=config.restart_policy,
        client_factory=lambda: fake_docker,
    )


@pytest.fixture
def stack(env_file, fake_docker):
    config = build_config(env_file)
    control = build_control(config, fake_docker)
    app = create_app(config=config, controller=control, settings=SettingsStore(env_file))
    return {"app": app, "config": config, "control": control, "docker": fake_docker}


def login(client: TestClient) -> None:
    response = client.post(
        "/login",
        data={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


# ---------------------------------------------------- first-run create path


def test_first_run_pulls_creates_and_starts(stack):
    control, docker = stack["control"], stack["docker"]
    assert control.status()["phase"] == "absent"
    assert control.status()["container_exists"] is False

    status = control.start()

    assert docker.images.pulls == [("ghcr.io/community-valheim-tools/valheim-server", "latest")]
    assert len(docker.containers.create_calls) == 1
    created = docker.containers.create_calls[0]
    assert created["image"] == IMAGE
    assert created["name"] == CONTAINER
    # Env comes from the settings file the compose stack feeds to `valheim`.
    assert created["environment"]["SERVER_NAME"] == "Midgard Test"
    assert created["environment"]["SERVER_PASS"] == "hunter22"
    # Game, query, and crossplay backend ports, derived from SERVER_PORT.
    assert created["ports"] == {"2456/udp": 2456, "2457/udp": 2457, "2458/udp": 2458}
    assert created["volumes"]["valheim-config"]["bind"] == "/config"
    assert created["volumes"]["valheim-data"]["bind"] == "/opt/valheim"
    assert status["container_state"] == "running"
    # Running is not ready: no readiness line has been emitted yet.
    assert status["phase"] == "running"
    assert status["ready"] is False


def test_create_kwargs_are_all_accepted_by_the_real_docker_library(stack):
    """The fake engine takes any kwarg; docker-py does not.

    ``containers.create`` validates its arguments against the model layer's own
    allow-lists and raises ``run() got an unexpected keyword argument ...`` before
    any request reaches the daemon. ``stop_timeout`` was exactly that trap: the
    Engine API accepts it, the model layer does not, and the whole suite stayed
    green while the first real Start failed. This pins every kwarg we send against
    the installed library rather than against the fake.
    """
    from docker.models.containers import RUN_CREATE_KWARGS, RUN_HOST_CONFIG_KWARGS

    control, docker = stack["control"], stack["docker"]
    control.start()
    created = dict(docker.containers.create_calls[0])
    created.pop("image")  # passed positionally, not as a kwarg

    # docker-py pops these out of kwargs itself before its allow-list check.
    handled_separately = {"ports", "volumes", "network", "networking_config"}
    accepted = set(RUN_CREATE_KWARGS) | set(RUN_HOST_CONFIG_KWARGS) | handled_separately

    rejected = sorted(set(created) - accepted)
    assert not rejected, (
        f"containers.create() would raise on {rejected}: the installed docker-py does "
        "not accept these. Check RUN_CREATE_KWARGS / RUN_HOST_CONFIG_KWARGS."
    )


def test_first_run_skips_pull_when_image_already_present(env_file):
    docker = FakeDockerClient(images={IMAGE})
    config = build_config(env_file)
    control = build_control(config, docker)

    control.start()

    assert docker.images.pulls == []
    assert len(docker.containers.create_calls) == 1


def test_start_failure_after_create_leaves_no_half_created_container(stack):
    control, docker = stack["control"], stack["docker"]
    docker.start_error = APIError("driver failed programming external connectivity")

    with pytest.raises(DockerControlError) as excinfo:
        control.start()

    assert "driver failed" in excinfo.value.docker_message
    # Nothing left behind, so the next Start is a clean first run again.
    assert docker.containers.registry == {}
    assert control.status()["container_exists"] is False


def test_pull_failure_surfaces_docker_error_and_creates_nothing(stack):
    control, docker = stack["control"], stack["docker"]
    docker.images.pull_error = APIError("manifest unknown")

    with pytest.raises(DockerControlError) as excinfo:
        control.start()

    assert "manifest unknown" in excinfo.value.docker_message
    assert docker.containers.create_calls == []
    assert control.status()["phase"] == "absent"


def test_existing_stopped_container_is_started_not_recreated(stack):
    control, docker = stack["control"], stack["docker"]
    docker.seed_stopped()
    docker.containers.create_calls.clear()

    status = control.start()

    assert docker.containers.create_calls == []
    assert docker.images.pulls == []
    assert status["container_state"] == "running"


# ------------------------------------------------------- readiness handling


def test_readiness_never_arrives_stays_running_not_ready(stack):
    control, docker = stack["control"], stack["docker"]
    docker.seed_running(
        log_messages=[
            "Steamworks initialized",
            "DungeonDB Start 1",
            "Loading world Dedicated",
        ]
    )

    status = control.status()

    assert status["container_state"] == "running"
    assert status["phase"] == "running"
    assert status["ready"] is False
    assert "not yet ready" in status["message"]


def advance_monotonic(monkeypatch, seconds: float) -> None:
    """Push docker_control's clock forward, so a throttled rescan is due again."""
    import app.docker_control as docker_control_module

    base = time.monotonic()
    monkeypatch.setattr(
        docker_control_module.time, "monotonic", lambda: base + seconds
    )


def test_readiness_line_flips_running_to_ready(stack, monkeypatch):
    control, docker = stack["control"], stack["docker"]
    container = docker.seed_running(log_messages=["Loading world Dedicated"])
    assert control.status()["phase"] == "running"

    container.log_messages.append(READY_LINE)
    # The negative scan is throttled, so a poll-driven UI would learn this from the
    # log pump; a caller relying on the rescan alone waits out the interval.
    advance_monotonic(monkeypatch, control.ready_rescan_seconds + 1)
    status = control.status()

    assert status["phase"] == "ready"
    assert status["ready"] is True


def test_a_ready_line_seen_by_the_log_pump_flips_readiness_without_a_rescan(stack):
    """The throttle must not delay the UI: the pump marks readiness as lines arrive."""
    control, docker = stack["control"], stack["docker"]
    container = docker.seed_running(log_messages=["Loading world Dedicated"])
    assert control.status()["ready"] is False

    container.log_messages.append(READY_LINE)
    control.fetch_logs()  # what _pump does every poll

    assert control.status()["ready"] is True


def test_a_run_that_never_reports_ready_is_not_rescanned_on_every_poll(stack):
    """The scan reads the whole run's log, and only the positive answer is cached --
    so an unthrottled negative is one full-log fetch every couple of seconds, per open
    tab, against a file with no size cap."""
    control, docker = stack["control"], stack["docker"]
    container = docker.seed_running(log_messages=["Loading world Dedicated"])

    for _ in range(10):
        assert control.status()["ready"] is False

    assert len(container.logs_calls) == 1


def test_the_rescan_resumes_once_the_interval_has_passed(stack, monkeypatch):
    control, docker = stack["control"], stack["docker"]
    container = docker.seed_running(log_messages=["Loading world Dedicated"])
    control.status()
    assert len(container.logs_calls) == 1

    advance_monotonic(monkeypatch, control.ready_rescan_seconds + 1)
    control.status()

    assert len(container.logs_calls) == 2


def test_readiness_does_not_leak_across_runs_from_a_stale_log_window(stack):
    """_pump keeps a `since` from before a restart, so it hands the controller lines
    that predate the current run. Matching one would claim "ready" mid world-load."""
    from app.docker_control import LogLine

    control, docker = stack["control"], stack["docker"]
    container = docker.seed_running()
    # The new run started after the line below was written.
    container.started_at = _iso(LOG_EPOCH_BASE + 500)
    stale = LogLine(
        epoch=LOG_EPOCH_BASE + 100, raw=f"old {READY_LINE}", message=READY_LINE
    )

    assert control.mark_ready_if_match(container, [stale]) is False
    assert control.status()["ready"] is False

    # A line from this run still counts.
    fresh = LogLine(
        epoch=LOG_EPOCH_BASE + 600, raw=f"new {READY_LINE}", message=READY_LINE
    )
    assert control.mark_ready_if_match(container, [fresh]) is True


def test_readiness_resets_when_the_container_restarts(stack):
    control, docker = stack["control"], stack["docker"]
    container = docker.seed_running(log_messages=[READY_LINE])
    assert control.status()["ready"] is True

    # A restart produces a new StartedAt, which is part of the readiness cache key.
    container.log_messages = ["Loading world Dedicated"]
    container.started_at = "2026-09-12T19:30:00.000000000Z"

    status = control.status()
    assert status["ready"] is False
    assert status["phase"] == "running"


def test_readiness_scan_failure_does_not_claim_ready(stack):
    control, docker = stack["control"], stack["docker"]
    docker.seed_running(log_messages=[READY_LINE])
    docker.logs_error = APIError("log driver does not support reading")

    status = control.status()

    assert status["ready"] is False
    assert status["phase"] == "running"


# -------------------------------------------------------------------- stop


def test_graceful_stop_uses_the_configured_timeout(stack):
    control, docker = stack["control"], stack["docker"]
    container = docker.seed_running()

    status = control.stop()

    assert container.stop_calls == [7]
    assert container.kill_calls == 0
    assert status["phase"] == "stopped"


def test_stop_timeout_offers_a_force_stop_then_kills(stack):
    control, docker = stack["control"], stack["docker"]
    container = docker.seed_running()
    docker.stop_error = APIError("Read timed out")

    with pytest.raises(DockerControlError) as excinfo:
        control.stop()
    assert excinfo.value.force_available is True

    docker.stop_error = None
    control.stop(force=True)
    assert container.kill_calls == 1


# -------------------------------------------------- unauthenticated access


def test_unauthenticated_dashboard_redirects_to_login(stack):
    with TestClient(stack["app"]) as client:
        response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def test_login_page_exposes_no_status_controls_or_logs(stack):
    with TestClient(stack["app"]) as client:
        body = client.get("/login").text
    for leak in ("Live console", "btn-start", "Current settings", "Midgard Test"):
        assert leak not in body


def test_unauthenticated_api_is_blocked_and_touches_no_docker(stack):
    docker = stack["docker"]
    with TestClient(stack["app"]) as client:
        assert client.get("/api/status").status_code == 401
        for path in ("/api/start", "/api/stop", "/api/restart"):
            response = client.post(path, headers={"Origin": ORIGIN}, json={})
            assert response.status_code == 401, path
            assert response.json()["error"] == "Authentication required."
    assert docker.containers.create_calls == []
    assert docker.containers.registry == {}


def test_unauthenticated_websocket_is_refused(stack):
    with TestClient(stack["app"]) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/logs"):
                pass  # pragma: no cover - the handshake must fail


def test_wrong_credentials_give_a_generic_error_without_enumeration(stack):
    with TestClient(stack["app"]) as client:
        bad_user = client.post(
            "/login",
            data={"username": "loki", "password": ADMIN_PASSWORD},
            headers={"Origin": ORIGIN},
        )
        bad_password = client.post(
            "/login",
            data={"username": ADMIN_USER, "password": "nope"},
            headers={"Origin": ORIGIN},
        )
    assert bad_user.status_code == bad_password.status_code == 401
    assert "Invalid username or password." in bad_user.text
    assert bad_user.text == bad_password.text
    assert "valheim_manager_session" not in bad_user.headers.get("set-cookie", "")


# ------------------------------------------------------- origin enforcement


def test_foreign_origin_is_rejected_before_any_docker_action(stack):
    docker = stack["docker"]
    with TestClient(stack["app"]) as client:
        login(client)
        for path in ("/api/start", "/api/stop", "/api/restart"):
            response = client.post(path, headers={"Origin": "http://evil.example"}, json={})
            assert response.status_code == 403, path
            assert "cross-site" in response.json()["error"]
    # Nothing on the container changed.
    assert docker.containers.create_calls == []
    assert docker.containers.registry == {}


def test_absent_origin_is_rejected(stack):
    docker = stack["docker"]
    with TestClient(stack["app"]) as client:
        login(client)
        response = client.post("/api/start", json={})
    assert response.status_code == 403
    assert "missing Origin" in response.json()["error"]
    assert docker.containers.create_calls == []


def test_same_origin_control_request_is_accepted(stack):
    with TestClient(stack["app"]) as client:
        login(client)
        response = client.post("/api/start", headers={"Origin": ORIGIN}, json={})
    assert response.status_code == 200
    assert response.json()["status"]["container_state"] == "running"


def test_explicitly_allowed_origin_is_accepted(env_file, fake_docker):
    config = build_config(env_file, allowed_origins=["https://valheim.lan"])
    control = build_control(config, fake_docker)
    app = create_app(config=config, controller=control, settings=SettingsStore(env_file))
    with TestClient(app) as client:
        login(client)
        response = client.post("/api/start", headers={"Origin": "https://valheim.lan"}, json={})
    assert response.status_code == 200


# ------------------------------------------------------------- session life


def test_session_survives_a_manager_restart(env_file, fake_docker):
    config = build_config(env_file)
    first = create_app(
        config=config, controller=build_control(config, fake_docker), settings=SettingsStore(env_file)
    )
    with TestClient(first) as client:
        login(client)
        cookie = client.cookies["valheim_manager_session"]

    # Fresh app object == restarted manager container. Same SESSION_SECRET.
    second_config = build_config(env_file)
    second = create_app(
        config=second_config,
        controller=build_control(second_config, fake_docker),
        settings=SettingsStore(env_file),
    )
    with TestClient(second) as client:
        client.cookies.set("valheim_manager_session", cookie)
        assert client.get("/", follow_redirects=False).status_code == 200
        assert client.get("/api/status").status_code == 200


def test_session_cookie_is_httponly_and_samesite_strict(stack):
    with TestClient(stack["app"]) as client:
        response = client.post(
            "/login",
            data={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
    header = response.headers["set-cookie"]
    assert "HttpOnly" in header
    assert "SameSite=strict" in header.replace("samesite", "SameSite")


def test_session_from_a_different_secret_is_rejected(env_file, fake_docker):
    config = build_config(env_file)
    app = create_app(
        config=config, controller=build_control(config, fake_docker), settings=SettingsStore(env_file)
    )
    with TestClient(app) as client:
        login(client)
        cookie = client.cookies["valheim_manager_session"]

    rotated = build_config(env_file, session_secret="a-totally-different-secret-98765")
    other = create_app(
        config=rotated, controller=build_control(rotated, fake_docker), settings=SettingsStore(env_file)
    )
    with TestClient(other) as client:
        client.cookies.set("valheim_manager_session", cookie)
        assert client.get("/", follow_redirects=False).status_code == 302


# ------------------------------------------------------------ log streaming


def test_websocket_pushes_status_then_backfills_and_appends_lines(stack):
    docker = stack["docker"]
    container = docker.seed_running(log_messages=["Loading world Dedicated"])
    with TestClient(stack["app"]) as client:
        login(client)
        with client.websocket_connect("/ws/logs") as ws:
            first = ws.receive_json()
            assert first["type"] == "status"
            assert first["status"]["phase"] == "running"
            backfill = _next_log(ws)
            assert "Loading world Dedicated" in backfill

            container.log_messages.append(READY_LINE)
            appended = _collect_logs(ws, needle="Game server connected")
            assert any("Game server connected" in line for line in appended)
            # Backfilled lines are not re-sent on subsequent polls.
            assert not any("Loading world Dedicated" in line for line in appended)


def test_websocket_reports_status_error_instead_of_crashing(stack):
    docker = stack["docker"]
    docker.seed_running()
    docker.logs_error = APIError("log driver does not support reading")
    with TestClient(stack["app"]) as client:
        login(client)
        with client.websocket_connect("/ws/logs") as ws:
            assert ws.receive_json()["type"] == "status"
            message = ws.receive_json()
            assert message["type"] == "log_error"
            assert "Could not read the container log." in message["error"]


def _next_log(ws, attempts: int = 40) -> list[str]:
    for _ in range(attempts):
        message = ws.receive_json()
        if message["type"] == "log":
            return message["lines"]
    raise AssertionError("no log frame arrived")  # pragma: no cover


def _collect_logs(ws, *, needle: str, attempts: int = 60) -> list[str]:
    collected: list[str] = []
    for _ in range(attempts):
        message = ws.receive_json()
        if message["type"] != "log":
            continue
        collected.extend(message["lines"])
        if any(needle in line for line in collected):
            return collected
    raise AssertionError(f"never saw {needle!r}")  # pragma: no cover


# ------------------------------------------------------------- settings read


def test_settings_are_parsed_and_secrets_masked(stack, env_file):
    store = SettingsStore(env_file)
    values = store.read()
    assert values["SERVER_NAME"] == "Midgard Test"
    assert values["SERVER_PASS"] == "hunter22"
    assert values["SERVER_PUBLIC"] == "1"  # `export ` prefix handled
    assert values["TZ"] == "Etc/UTC"  # inline comment stripped

    rows = {row["key"]: row["value"] for row in store.display_settings()}
    assert rows["SERVER_PASS"] == MASK
    assert rows["SERVER_NAME"] == "Midgard Test"

    with TestClient(stack["app"]) as client:
        login(client)
        body = client.get("/").text
        payload = client.get("/api/status").json()
    assert "hunter22" not in body
    assert "hunter22" not in client.get("/login").text
    assert all(row["value"] != "hunter22" for row in payload["settings"])


def test_missing_settings_file_is_reported_not_crashed(tmp_path):
    """The store still names a missing file. The app no longer reaches this state on
    its own -- boot creates the defaults -- but an unwritable settings directory
    leaves the file absent, and the UI has to say so rather than crash."""
    missing = tmp_path / "nope.env"

    with pytest.raises(SettingsFileError) as excinfo:
        SettingsStore(missing).read()

    assert "not found" in str(excinfo.value)


def test_boot_creates_the_default_settings_file_when_none_exists(tmp_path, fake_docker):
    """A clean install has to have something to show and something to run with."""
    settings_path = tmp_path / "settings" / "valheim.env"
    config = build_config(settings_path)
    app = create_app(
        config=config,
        controller=build_control(config, fake_docker),
        settings=SettingsStore(settings_path),
    )

    assert settings_path.read_text(encoding="utf-8") == DEFAULT_SETTINGS_TEXT
    with TestClient(app) as client:
        login(client)
        payload = client.get("/api/status").json()
    assert payload["settings_error"] is None
    rows = {row["key"]: row["value"] for row in payload["settings"]}
    assert rows["WORLD_NAME"] == "Dedicated"
    # Shipped empty, so an unedited default still cannot boot a public server.
    assert rows["SERVER_PASS"] == ""


# -------------------------------------------------------- password hashing


def test_bcrypt_and_argon2_hashes_verify():
    for algo in ("bcrypt", "argon2"):
        digest = hash_password("a-long-enough-password", algo=algo, rounds=4)
        auth = SessionAuth(
            admin_user=ADMIN_USER,
            admin_password_hash=digest,
            session_secret=SESSION_SECRET,
        )
        assert auth.check_credentials(ADMIN_USER, "a-long-enough-password") is True
        assert auth.check_credentials(ADMIN_USER, "wrong") is False
        assert auth.check_credentials("someone-else", "a-long-enough-password") is False


@pytest.mark.parametrize(
    "bad_hash",
    [
        "",
        # sha256 of "password" -- a bare digest must never be accepted
        "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8",
        "plaintext-password",
    ],
)
def test_non_bcrypt_argon2_hashes_are_refused(bad_hash):
    with pytest.raises(AuthConfigError):
        validate_password_hash(bad_hash)
    with pytest.raises(AuthConfigError):
        SessionAuth(
            admin_user=ADMIN_USER, admin_password_hash=bad_hash, session_secret=SESSION_SECRET
        )


# ------------------------------------- regression: the Compose `$` truncation

# What Compose leaves behind when a hash is placed in `.env` (or in a plain
# `env_file:`) instead of in `manager.env`: `$12` and the digest tail are read as
# variable references and replaced with blank strings. Reproduced with
# `docker compose config` on v2.38.1 against both hash formats.
TRUNCATED_BCRYPT = "$2b$12"
TRUNCATED_ARGON2 = "$argon2id$v=19$m=65536,t=3,p=4"
# argon2 fares worse: `$argon2id` and `$v` are themselves valid variable names, so
# the recognisable prefix disappears completely.
MANGLED_ARGON2 = "=19=65536,t=3,p=4+b+dWRWJTmaaJObG"


@pytest.mark.parametrize(
    "wrecked",
    [TRUNCATED_BCRYPT, "$2b$12$", "$2y$10$tooshort", TRUNCATED_ARGON2, MANGLED_ARGON2],
)
def test_compose_truncated_hashes_are_rejected_at_construction(wrecked):
    """A prefix-only check would accept these, and login would then fail forever."""
    with pytest.raises(AuthConfigError) as excinfo:
        SessionAuth(
            admin_user=ADMIN_USER, admin_password_hash=wrecked, session_secret=SESSION_SECRET
        )
    message = str(excinfo.value)
    # The error must name the real cause, not just "not recognised".
    assert "manager.env" in message
    assert "Compose" in message
    assert "$" in message

    # And it must never be treated as a usable hash.
    with pytest.raises(AuthConfigError):
        validate_password_hash(wrecked)
    assert verify_password("anything", wrecked) is False


def test_quoted_hash_is_rejected_with_the_raw_format_explanation():
    """`format: raw` keeps quotes, so a quoted value is a real failure mode."""
    quoted = f"'{ADMIN_HASH}'"
    with pytest.raises(AuthConfigError) as excinfo:
        SessionAuth(
            admin_user=ADMIN_USER, admin_password_hash=quoted, session_secret=SESSION_SECRET
        )
    assert "quotes" in str(excinfo.value)
    assert "format: raw" in str(excinfo.value)


def test_intact_hashes_still_validate_and_verify():
    """The structural check must not reject the real thing."""
    for algo in ("bcrypt", "argon2"):
        digest = hash_password("a-long-enough-password", algo=algo, rounds=4)
        assert validate_password_hash(digest) == algo
        assert verify_password("a-long-enough-password", digest) is True
        assert verify_password("wrong", digest) is False

    # Fixed samples, so a regression cannot hide behind freshly generated hashes.
    real_bcrypt = "$2b$12$ybKIasQ9nE.NVP6n33Nv8eiRWU6UAYe7GiHgFsYyzO7GkcvzVZPoC"
    real_argon2 = (
        "$argon2id$v=19$m=65536,t=3,p=4"
        "$c29tZXNhbHRzb21lc2FsdA$RdescudvJCsgt3ub+b+dWRWJTmaaJObGImtBCyLM1G4"
    )
    assert validate_password_hash(real_bcrypt) == "bcrypt"
    assert validate_password_hash(real_argon2) == "argon2"
    # A bcrypt hash is exactly 60 characters; truncation is what we are guarding against.
    assert len(real_bcrypt) == 60


def test_weak_or_missing_session_secret_refuses_to_start():
    for secret in ("", "short"):
        with pytest.raises(AuthConfigError):
            SessionAuth(
                admin_user=ADMIN_USER, admin_password_hash=ADMIN_HASH, session_secret=secret
            )


def test_missing_admin_user_refuses_to_start():
    with pytest.raises(AuthConfigError):
        SessionAuth(
            admin_user="  ", admin_password_hash=ADMIN_HASH, session_secret=SESSION_SECRET
        )


# ----------------------------------------------------- docker error surfacing


def test_docker_proxy_unreachable_is_surfaced_not_swallowed(env_file):
    config = build_config(env_file)
    store = SettingsStore(env_file)

    def boom():
        raise APIError("Connection refused to docker-socket-proxy:2375")

    control = DockerControl(
        base_url=config.docker_host,
        container_name=CONTAINER,
        image=IMAGE,
        network=None,
        config_volume="c",
        data_volume="d",
        env_provider=store.container_env,
        port_provider=lambda: [2456],
        ready_pattern=config.ready_pattern,
        client_factory=boom,
    )
    app = create_app(config=config, controller=control, settings=store)
    with TestClient(app) as client:
        login(client)
        payload = client.get("/api/status").json()
        action = client.post("/api/start", headers={"Origin": ORIGIN}, json={})
    assert payload["status"]["phase"] == "error"
    assert action.status_code == 502
    assert "Connection refused" in action.json()["docker_error"]


# =====================================================================
# Surfaces the design leans on that previously had no coverage.
# =====================================================================

# --------------------------------------------------- (1) restart actually runs


def test_restart_stops_then_starts_a_running_container(stack):
    control, docker = stack["control"], stack["docker"]
    container = docker.seed_running(log_messages=[READY_LINE])
    assert control.status()["phase"] == "ready"

    status = control.restart()

    assert container.stop_calls == [7]
    # The whole point: it must come back up, not just go down.
    assert status["container_state"] == "running"
    assert status["phase"] == "running"
    # A new run, so readiness starts over rather than being inherited.
    assert status["ready"] is False


def test_restart_with_no_container_is_a_first_run(stack):
    control, docker = stack["control"], stack["docker"]

    status = control.restart()

    assert len(docker.containers.create_calls) == 1
    assert status["container_state"] == "running"


def test_restart_endpoint_starts_the_server(stack):
    with TestClient(stack["app"]) as client:
        login(client)
        response = client.post("/api/restart", headers={"Origin": ORIGIN}, json={})
    assert response.status_code == 200
    assert response.json()["status"]["container_state"] == "running"


# ------------------------------------ (2) transient phases are really reported


def test_pull_create_start_phases_are_observable_while_in_flight(stack):
    """The manager owns the container *in order to* report these phases."""
    control, docker = stack["control"], stack["docker"]
    seen: list[str] = []

    docker.pull_hook = lambda: seen.append(control.status()["phase"])
    docker.create_hook = lambda: seen.append(control.status()["phase"])
    docker.start_hook = lambda: seen.append(control.status()["phase"])

    control.start()

    assert seen == ["pulling", "creating", "starting"]
    # And the transient phase is gone once the work is done.
    assert control.status()["phase"] == "running"


def test_pull_phase_message_names_the_image(stack):
    control, docker = stack["control"], stack["docker"]
    captured: list[dict] = []
    docker.pull_hook = lambda: captured.append(control.status())

    control.start()

    assert captured[0]["phase"] == "pulling"
    assert IMAGE in captured[0]["message"]
    assert captured[0]["container_exists"] is False


def test_a_failed_action_does_not_leave_a_stuck_phase(stack):
    control, docker = stack["control"], stack["docker"]
    docker.images.pull_error = APIError("manifest unknown")

    with pytest.raises(DockerControlError):
        control.start()

    assert control.status()["phase"] == "absent"


def test_pull_phase_ttl_outlives_a_slow_first_run_pull():
    """A ~1GB pull can take well over 15 minutes; expiring mid-pull would report
    absent, re-enable Start, and let a second create 409."""
    from app.docker_control import _ACTION_TTL, _DEFAULT_ACTION_TTL, PHASE_PULLING

    assert _ACTION_TTL[PHASE_PULLING] > _DEFAULT_ACTION_TTL
    assert _ACTION_TTL[PHASE_PULLING] >= 3600


# ------------------------------------------- (3) force stop through the API


def test_force_stop_via_the_api_kills_the_container(stack):
    """Exercises the request-body parsing in main.py, not just the controller."""
    docker = stack["docker"]
    container = docker.seed_running()
    docker.stop_error = APIError("Read timed out")

    with TestClient(stack["app"]) as client:
        login(client)
        graceful = client.post("/api/stop", headers={"Origin": ORIGIN}, json={})
        assert graceful.status_code == 502
        assert graceful.json()["force_available"] is True
        assert container.kill_calls == 0

        forced = client.post("/api/stop", headers={"Origin": ORIGIN}, json={"force": True})

    assert forced.status_code == 200
    assert container.kill_calls == 1


def test_stop_without_a_body_is_a_graceful_stop(stack):
    docker = stack["docker"]
    container = docker.seed_running()
    with TestClient(stack["app"]) as client:
        login(client)
        response = client.post("/api/stop", headers={"Origin": ORIGIN}, content=b"")
    assert response.status_code == 200
    assert container.stop_calls == [7]
    assert container.kill_calls == 0


# ---------------------------------------------------------- (4) logout works


def test_logout_clears_the_session_and_locks_the_ui_again(stack):
    with TestClient(stack["app"]) as client:
        login(client)
        assert client.get("/", follow_redirects=False).status_code == 200

        response = client.post("/logout", headers={"Origin": ORIGIN}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        # Without Path=/ the browser keeps the cookie and sign-out silently fails.
        assert "Path=/" in response.headers["set-cookie"]

        assert client.get("/", follow_redirects=False).status_code == 302
        assert client.get("/api/status").status_code == 401


def test_logout_requires_a_same_origin_request(stack):
    with TestClient(stack["app"]) as client:
        login(client)
        response = client.post(
            "/logout", headers={"Origin": "http://evil.example"}, follow_redirects=False
        )
        assert response.status_code == 403
        # Still signed in -- the rejection changed nothing.
        assert client.get("/", follow_redirects=False).status_code == 200


# ------------------------------------------------------- (5) session expiry


def _issue_token_aged(monkeypatch, auth, seconds_ago: int) -> str:
    """Issue a session token stamped ``seconds_ago`` in the past."""
    from itsdangerous.timed import TimestampSigner

    original = TimestampSigner.get_timestamp
    monkeypatch.setattr(
        TimestampSigner,
        "get_timestamp",
        lambda self: original(self) - seconds_ago,
    )
    token = auth.issue_token(ADMIN_USER)
    monkeypatch.undo()
    return token


def test_session_cookie_expires_after_max_age(monkeypatch):
    auth = SessionAuth(
        admin_user=ADMIN_USER,
        admin_password_hash=ADMIN_HASH,
        session_secret=SESSION_SECRET,
        max_age_seconds=60,
    )
    stale = _issue_token_aged(monkeypatch, auth, 3600)

    assert auth.read_token(stale) is None
    # A freshly issued one is still fine, so this pins max_age and nothing else.
    assert auth.read_token(auth.issue_token(ADMIN_USER)) is not None


def test_expired_cookie_is_refused_by_the_app(stack, monkeypatch):
    auth = stack["app"].state.auth
    stale = _issue_token_aged(monkeypatch, auth, auth.max_age_seconds + 3600)

    with TestClient(stack["app"]) as client:
        client.cookies.set("valheim_manager_session", stale)
        assert client.get("/", follow_redirects=False).status_code == 302
        assert client.get("/api/status").status_code == 401


# -------------------------------------------------------- (6) COOKIE_SECURE


def test_cookie_secure_flag_is_honoured(env_file, fake_docker):
    config = build_config(env_file, cookie_secure=True)
    app = create_app(
        config=config,
        controller=build_control(config, fake_docker),
        settings=SettingsStore(env_file),
    )
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/login",
            data={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
            headers={"Origin": "https://testserver"},
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert "Secure" in response.headers["set-cookie"]


def test_cookie_is_not_secure_when_the_flag_is_off(stack):
    with TestClient(stack["app"]) as client:
        response = client.post(
            "/login",
            data={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
    assert "Secure" not in response.headers["set-cookie"]


# --------------------------------------------------------------- (7) healthz


def test_healthz_is_unauthenticated_and_leaks_nothing(stack):
    """Backs the compose healthcheck, so it must answer without a session."""
    with TestClient(stack["app"]) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ------------------------------------------- (8) the full container create spec


def test_create_call_carries_the_whole_container_spec(stack):
    control, docker = stack["control"], stack["docker"]

    control.start()

    created = docker.containers.create_calls[0]
    assert created["restart_policy"] == {"Name": "unless-stopped"}
    assert created["network"] == "test-net"
    assert created["cap_add"] == ["sys_nice"]
    assert created["tty"] is False
    assert created["labels"] == {"com.valheim-manager.managed": "true"}
    # Deliberately NOT stop_timeout: docker-py's model layer rejects it outright
    # (see test_create_kwargs_are_all_accepted_by_the_real_docker_library). The
    # grace period is applied by stop() instead, which is asserted separately in
    # test_graceful_stop_uses_the_configured_timeout.
    assert "stop_timeout" not in created


def test_restart_policy_no_is_passed_as_no_policy(env_file, fake_docker):
    config = build_config(env_file, restart_policy="no")
    control = build_control(config, fake_docker)

    control.start()

    assert fake_docker.containers.create_calls[0]["restart_policy"] is None


# ------------------------------------------------- (9) every phase-map branch


@pytest.mark.parametrize(
    "docker_state,expected_phase",
    [
        ("running", "running"),
        ("restarting", "starting"),
        ("paused", "paused"),
        ("removing", "stopping"),
        ("created", "stopped"),
        ("exited", "stopped"),
        ("dead", "stopped"),
    ],
)
def test_every_docker_state_maps_to_a_truthful_phase(stack, docker_state, expected_phase):
    """A crash-looping server must not read as a calm stopped with Start enabled."""
    control, docker = stack["control"], stack["docker"]
    container = docker.seed_running()
    container.status = docker_state

    status = control.status()

    assert status["phase"] == expected_phase
    assert status["container_state"] == docker_state
    assert status["container_exists"] is True
    # Only a genuinely running container may ever be called ready.
    if docker_state != "running":
        assert status["ready"] is False


def test_dead_container_reports_its_exit_code(stack):
    control, docker = stack["control"], stack["docker"]
    container = docker.seed_running()
    container.status = "exited"
    container.exit_code = 137

    status = control.status()

    assert status["phase"] == "stopped"
    assert status["exit_code"] == 137


# =====================================================================
# Regressions for the review fixes.
# =====================================================================

# ------------------------------------------------------- image reference split


@pytest.mark.parametrize(
    "image,expected",
    [
        ("valheim-server", ("valheim-server", "latest")),
        ("ghcr.io/org/valheim-server", ("ghcr.io/org/valheim-server", "latest")),
        ("ghcr.io/org/valheim-server:1.2.0", ("ghcr.io/org/valheim-server", "1.2.0")),
        # A registry port is not a tag.
        ("registry.lan:5000/valheim", ("registry.lan:5000/valheim", "latest")),
        ("registry.lan:5000/valheim:2.0", ("registry.lan:5000/valheim", "2.0")),
        # Digest-pinned: rpartition(":") alone would yield ("repo@sha256", "abc...")
        # and the first Start would fail on a bogus repository.
        (
            "ghcr.io/org/valheim@sha256:" + "a" * 64,
            ("ghcr.io/org/valheim", "sha256:" + "a" * 64),
        ),
    ],
)
def test_split_image_ref(image, expected):
    from app.docker_control import _split_image_ref

    assert _split_image_ref(image) == expected


def test_digest_pinned_image_is_pulled_by_digest(env_file):
    digest_image = "ghcr.io/org/valheim@sha256:" + "b" * 64
    docker = FakeDockerClient()
    config = build_config(env_file, image=digest_image)
    control = build_control(config, docker)

    control.start()

    assert docker.images.pulls == [("ghcr.io/org/valheim", "sha256:" + "b" * 64)]
    assert docker.containers.create_calls[0]["image"] == digest_image


# ------------------------------------ transport errors become DockerControlError


@pytest.mark.parametrize(
    "boom_factory",
    [
        # requests raises these and neither is a DockerException, so without the
        # mapping they escape as a raw 500 and kill the log pump.
        lambda: requests.exceptions.ReadTimeout("read timed out"),
        lambda: requests.exceptions.ConnectionError("proxy refused"),
        lambda: RuntimeError("something unexpected"),
    ],
)
def test_transport_errors_surface_as_docker_control_errors(stack, boom_factory):
    control, docker = stack["control"], stack["docker"]
    docker.get_error = boom_factory()

    with pytest.raises(DockerControlError):
        control.get_container()
    with pytest.raises(DockerControlError):
        control.fetch_logs()


def test_hung_proxy_becomes_an_error_status_not_a_500(stack):
    docker = stack["docker"]
    docker.get_error = requests.exceptions.ReadTimeout("read timed out")
    with TestClient(stack["app"]) as client:
        login(client)
        status = client.get("/api/status")
        action = client.post("/api/start", headers={"Origin": ORIGIN}, json={})
    assert status.status_code == 200
    assert status.json()["status"]["phase"] == "error"
    assert action.status_code == 502


def test_log_read_failure_surfaces_on_the_websocket_without_killing_it(stack):
    docker = stack["docker"]
    docker.seed_running()
    docker.logs_error = requests.exceptions.ReadTimeout("read timed out")
    with TestClient(stack["app"]) as client:
        login(client)
        with client.websocket_connect("/ws/logs") as ws:
            assert ws.receive_json()["type"] == "status"
            assert ws.receive_json()["type"] == "log_error"
            # Still alive: the pump keeps pushing status afterwards.
            assert ws.receive_json()["type"] == "status"


def test_reload_failure_after_start_is_reported_not_raised_raw(stack):
    control, docker = stack["control"], stack["docker"]
    container = docker.seed_stopped()
    container.reload_error = RuntimeError("connection reset")

    with pytest.raises(DockerControlError) as excinfo:
        control.start()
    assert "re-inspect" in excinfo.value.message


# --------------------------------------------------- readiness scan is unbounded


def test_readiness_scan_covers_the_whole_run_not_a_line_window(stack):
    """A 400-line cap loses the ready line on a long-running server, stranding the
    UI on "not yet ready" forever after a manager restart."""
    control, docker = stack["control"], stack["docker"]
    container = docker.seed_running(log_messages=[READY_LINE] + ["chatter"] * 1000)

    assert control.status()["phase"] == "ready"

    scan = container.logs_calls[0]
    assert scan["tail"] == "all"
    assert scan["since"] is not None  # still bounded to this run


def test_readiness_scan_falls_back_to_the_line_cap_for_an_unstarted_run(stack):
    control, docker = stack["control"], stack["docker"]
    container = docker.seed_running()
    container.started_at = "0001-01-01T00:00:00Z"

    control.status()

    assert container.logs_calls[0]["tail"] == 400


# ------------------------------------------------------ numeric env validation


def test_log_poll_seconds_is_floored_to_avoid_a_busy_loop(monkeypatch):
    from app.main import MIN_LOG_POLL_SECONDS, config_from_env

    for value in ("0", "-1", "0.0001"):
        monkeypatch.setenv("LOG_POLL_SECONDS", value)
        assert config_from_env().log_poll_seconds == MIN_LOG_POLL_SECONDS


def test_log_tail_lines_and_status_interval_have_floors(monkeypatch):
    from app.main import config_from_env

    monkeypatch.setenv("LOG_TAIL_LINES", "0")
    monkeypatch.setenv("STATUS_EVERY_N_POLLS", "0")
    config = config_from_env()
    assert config.log_tail_lines == 1
    assert config.status_every_n_polls == 1


def test_status_every_n_polls_is_wired_to_its_env_var(monkeypatch):
    from app.main import config_from_env

    monkeypatch.setenv("STATUS_EVERY_N_POLLS", "5")
    assert config_from_env().status_every_n_polls == 5


def test_unusable_session_max_age_refuses_to_start(monkeypatch):
    from app.main import ConfigError, config_from_env

    for bad in ("0", "5", "-60"):
        monkeypatch.setenv("SESSION_MAX_AGE_SECONDS", bad)
        with pytest.raises(ConfigError) as excinfo:
            config_from_env()
        assert "SESSION_MAX_AGE_SECONDS" in str(excinfo.value)

    monkeypatch.setenv("SESSION_MAX_AGE_SECONDS", "not-a-number")
    with pytest.raises(ConfigError):
        config_from_env()

    monkeypatch.setenv("SESSION_MAX_AGE_SECONDS", "3600")
    assert config_from_env().session_max_age_seconds == 3600


# -------------------------------------------------- settings file edge cases


def test_server_port_outside_the_usable_range_falls_back(tmp_path):
    # 65534 is rejected too: the query and crossplay ports sit above it.
    for bad in ("0", "70000", "65534", "-1"):
        path = tmp_path / f"port-{bad}.env"
        path.write_text(f"SERVER_PORT={bad}\n", encoding="utf-8")
        assert SettingsStore(path).server_port() == 2456

    ok = tmp_path / "ok.env"
    ok.write_text("SERVER_PORT=2500\n", encoding="utf-8")
    assert SettingsStore(ok).server_port() == 2500


def test_settings_path_that_is_a_directory_names_the_real_cause(tmp_path):
    """Docker creates a directory for a missing bind-mount source, and the
    operator's cp then lands inside it."""
    as_dir = tmp_path / "valheim.env"
    as_dir.mkdir()

    with pytest.raises(SettingsFileError) as excinfo:
        SettingsStore(as_dir).read()

    message = str(excinfo.value)
    assert "directory" in message
    assert "valheim.env.example" in message


def test_directory_settings_path_shows_as_an_error_in_the_ui(tmp_path, fake_docker):
    as_dir = tmp_path / "valheim.env"
    as_dir.mkdir()
    config = build_config(as_dir)
    app = create_app(
        config=config,
        controller=build_control(config, fake_docker),
        settings=SettingsStore(as_dir),
    )
    with TestClient(app) as client:
        login(client)
        payload = client.get("/api/status").json()
    assert payload["settings"] == []
    assert "directory" in payload["settings_error"]


# =====================================================================
# Second review round.
# =====================================================================

# ------------------------------------------- bcrypt's 72-byte input limit


def test_bcrypt_truncates_past_72_bytes_so_long_passwords_are_refused():
    """Measured on bcrypt 4.2.1: hashpw accepts a longer password but only the
    first 72 bytes participate, so a different password sharing that prefix
    verifies too. Hashing one would hand the operator a weaker credential than
    they think they have."""
    import bcrypt

    long_password = "A" * 80
    shares_prefix = "A" * 72 + "-a-completely-different-tail"

    # The underlying library really does behave this way ...
    raw = bcrypt.hashpw(long_password.encode(), bcrypt.gensalt(rounds=4))
    assert bcrypt.checkpw(shares_prefix.encode(), raw) is True

    # ... so we refuse to produce such a hash at all.
    with pytest.raises(PasswordTooLongError) as excinfo:
        hash_password(long_password, algo="bcrypt", rounds=4)
    message = str(excinfo.value)
    assert "72" in message
    assert "argon2" in message


def test_bcrypt_accepts_exactly_72_bytes():
    digest = hash_password("A" * 72, algo="bcrypt", rounds=4)
    assert verify_password("A" * 72, digest) is True
    assert verify_password("A" * 71, digest) is False


def test_the_byte_limit_is_counted_in_bytes_not_characters():
    """A 30-character passphrase of 3-byte characters is 90 bytes."""
    multibyte = "世" * 30
    assert len(multibyte) == 30
    assert len(multibyte.encode("utf-8")) == 90
    with pytest.raises(PasswordTooLongError):
        hash_password(multibyte, algo="bcrypt", rounds=4)


def test_argon2_has_no_length_limit_and_does_not_truncate():
    long_password = "A" * 200
    digest = hash_password(long_password, algo="argon2")
    assert verify_password(long_password, digest) is True
    # The tail matters, unlike bcrypt.
    assert verify_password("A" * 72, digest) is False
    assert verify_password("A" * 199, digest) is False


def test_hash_password_tool_reports_the_limit_instead_of_a_traceback():
    import subprocess
    import sys
    from pathlib import Path

    tool = Path(__file__).resolve().parents[2] / "tools" / "hash_password.py"
    proc = subprocess.run(
        [sys.executable, str(tool), "--stdin"],
        input="A" * 80 + "\n",
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr
    assert "72" in proc.stderr
    assert "argon2" in proc.stderr
    assert "ADMIN_PASSWORD_HASH" not in proc.stdout

    # argon2 takes the same password happily.
    ok = subprocess.run(
        [sys.executable, str(tool), "--stdin", "--algo", "argon2"],
        input="A" * 80 + "\n",
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0
    assert "ADMIN_PASSWORD_HASH=$argon2" in ok.stdout


# ------------------------------------ the log socket re-checks the session


def _age_signer(monkeypatch, seconds: int):
    """Push the signer's clock forward so already-issued tokens look older."""
    from itsdangerous.timed import TimestampSigner

    original = TimestampSigner.get_timestamp
    monkeypatch.setattr(
        TimestampSigner, "get_timestamp", lambda self: original(self) + seconds
    )


def test_open_log_socket_is_closed_once_the_session_stops_validating(
    env_file, fake_docker, monkeypatch
):
    """Authorising only at the handshake would keep streaming logs to a client
    whose session has since expired -- for up to the 7-day default."""
    config = build_config(env_file, session_max_age_seconds=60)
    control = build_control(config, fake_docker)
    app = create_app(config=config, controller=control, settings=SettingsStore(env_file))
    fake_docker.seed_running(log_messages=["Loading world Dedicated"])

    with TestClient(app) as client:
        login(client)
        with client.websocket_connect("/ws/logs") as ws:
            # Streaming normally while the session is valid.
            assert ws.receive_json()["type"] == "status"

            # Now the token is older than max_age.
            _age_signer(monkeypatch, 3600)

            with pytest.raises(WebSocketDisconnect) as excinfo:
                for _ in range(50):
                    ws.receive_json()
    # 1008 is what the client turns into a redirect to /login.
    assert excinfo.value.code == 1008


def test_log_socket_stays_open_while_the_session_is_valid(stack):
    """The re-check must not evict a legitimate client."""
    docker = stack["docker"]
    docker.seed_running(log_messages=["Loading world Dedicated"])
    with TestClient(stack["app"]) as client:
        login(client)
        with client.websocket_connect("/ws/logs") as ws:
            statuses = 0
            for _ in range(12):
                if ws.receive_json()["type"] == "status":
                    statuses += 1
            assert statuses >= 3


# --------------------------------------- shipped defaults cannot go public


def test_shipped_valheim_example_cannot_start_a_public_server(project_root):
    """An operator who copies the example and forgets to edit it must not end up
    listed in the public browser behind a password published in this repo."""
    values = parse_env_text((project_root / "valheim.env.example").read_text(encoding="utf-8"))

    # Empty: the upstream image requires >= 5 characters and refuses to boot.
    assert values["SERVER_PASS"] == ""
    assert len(values["SERVER_PASS"]) < 5
    # And not listed publicly even if a password were supplied.
    assert values["SERVER_PUBLIC"] in ("0", "false", "False")


# =====================================================================
# Zero-edit install: the first-run setup wizard and the credential state file.
# =====================================================================

SETUP_FORM = {
    "admin_user": "odin",
    "password": "a-long-enough-password",
    "password_confirm": "a-long-enough-password",
    "hash_algo": "bcrypt",
    "server_name": "Midgard",
    "world_name": "Yggdrasil",
    "server_port": "2500",
    "server_pass": "hunter22",
    "server_public": "1",
    "crossplay": "1",
}


def blank_config(tmp_path, **overrides) -> AppConfig:
    """A manager with no credentials anywhere: no env vars, no state file."""
    return build_config(
        tmp_path / "settings" / "valheim.env",
        admin_user="",
        admin_password_hash="",
        session_secret="",
        state_file=str(tmp_path / "state" / "manager-state.json"),
        **overrides,
    )


def unconfigured(tmp_path, fake_docker, **overrides) -> dict:
    config = blank_config(tmp_path, **overrides)
    store = SettingsStore(config.env_file)
    states = StateStore(config.state_file)
    app = create_app(
        config=config,
        controller=build_control(config, fake_docker),
        settings=store,
        state_store=states,
    )
    return {
        "app": app,
        "config": config,
        "settings": store,
        "states": states,
        "token": app.state.setup.token,
        "docker": fake_docker,
    }


@pytest.fixture
def fresh(tmp_path, fake_docker):
    return unconfigured(tmp_path, fake_docker)


def complete_setup(client: TestClient, token: str, **overrides):
    data = {**SETUP_FORM, "token": token}
    data.update(overrides)
    return client.post(
        "/setup", data=data, headers={"Origin": ORIGIN}, follow_redirects=False
    )


# ------------------------------------------- an unconfigured manager leads to /setup


def test_unconfigured_boot_logs_a_setup_url_and_creates_default_settings(
    tmp_path, fake_docker, caplog
):
    with caplog.at_level(logging.INFO, logger="valheim_manager"):
        bundle = unconfigured(tmp_path, fake_docker)

    assert "FIRST-RUN SETUP REQUIRED" in caplog.text
    assert f"/setup?token={bundle['token']}" in caplog.text
    # The settings file exists before the operator has typed anything.
    assert (tmp_path / "settings" / "valheim.env").exists()
    # And no game container was touched by any of it.
    assert fake_docker.containers.create_calls == []


def test_manager_url_makes_the_logged_setup_link_clickable(tmp_path, fake_docker, caplog):
    with caplog.at_level(logging.INFO, logger="valheim_manager"):
        bundle = unconfigured(tmp_path, fake_docker, manager_url="http://valheim.lan:8080")
    assert f"http://valheim.lan:8080/setup?token={bundle['token']}" in caplog.text


@pytest.mark.parametrize("path", ["/", "/login", "/api/status", "/api/start"])
def test_every_other_route_redirects_to_setup_while_unconfigured(fresh, path):
    with TestClient(fresh["app"]) as client:
        response = client.request(
            "POST" if path.startswith("/api/") else "GET",
            path,
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
    assert response.status_code == 302
    assert response.headers["location"] == "/setup"


def test_healthz_and_static_still_answer_while_unconfigured(fresh):
    """The compose healthcheck must not restart a manager that is awaiting setup, and
    the wizard needs its own stylesheet."""
    with TestClient(fresh["app"]) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/static/style.css").status_code == 200


def test_unconfigured_websocket_is_refused(fresh):
    """The HTTP redirect gate cannot see a WebSocket scope, so it guards itself."""
    with TestClient(fresh["app"]) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/logs"):
                pass  # pragma: no cover - the handshake must fail


# ------------------------------------------------------------- the one-time token


@pytest.mark.parametrize("token", ["", "not-the-token", "x"])
def test_bad_or_missing_token_is_refused_and_creates_no_admin(fresh, token):
    with TestClient(fresh["app"]) as client:
        page = client.get("/setup", params={"token": token} if token else None)
        submitted = complete_setup(client, token)

    assert page.status_code == 403
    assert submitted.status_code == 403
    # Generic: it never says whether a token is outstanding.
    assert "Setup link not valid" in page.text
    for leak in ("wrong", "expired", "no token", fresh["token"]):
        assert leak not in page.text
    # Nothing was written and the wizard is still open.
    assert not os.path.exists(fresh["config"].state_file)
    assert fresh["app"].state.auth is None
    assert fresh["app"].state.setup.is_open() is True


def test_the_real_token_still_works_after_a_refusal(fresh):
    with TestClient(fresh["app"]) as client:
        assert complete_setup(client, "not-the-token").status_code == 403
        good = client.get("/setup", params={"token": fresh["token"]})
    assert good.status_code == 200
    assert "Set up the Valheim manager" in good.text


def test_token_is_not_persisted_so_a_restart_replaces_it(tmp_path, fake_docker):
    first = unconfigured(tmp_path, fake_docker)
    second = create_app(
        config=first["config"],
        controller=build_control(first["config"], fake_docker),
        settings=first["settings"],
        state_store=StateStore(first["config"].state_file),
    )
    assert second.state.setup.token != first["token"]
    with TestClient(second) as client:
        assert client.get("/setup", params={"token": first["token"]}).status_code == 403


# ---------------------------------------------------------------- completing setup


def test_completing_setup_writes_credentials_and_settings_and_signs_in(fresh):
    with TestClient(fresh["app"]) as client:
        response = complete_setup(client, fresh["token"])
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        # Landed authenticated: no second sign-in.
        assert client.get("/", follow_redirects=False).status_code == 200
        assert client.get("/api/status").status_code == 200

    document = json.loads(open(fresh["config"].state_file, encoding="utf-8").read())
    assert document["admin_user"] == "odin"
    assert document["setup_completed"] is True
    assert document["version"] == 1
    # A real hash, generated here -- not a placeholder and not the password.
    assert validate_password_hash(document["admin_password_hash"]) == "bcrypt"
    assert verify_password("a-long-enough-password", document["admin_password_hash"])
    # A secret the manager generated for itself.
    assert len(document["session_secret"]) >= 16

    values = SettingsStore(fresh["config"].env_file).read()
    assert values["SERVER_NAME"] == "Midgard"
    assert values["WORLD_NAME"] == "Yggdrasil"
    assert values["SERVER_PORT"] == "2500"
    assert values["SERVER_PASS"] == "hunter22"
    assert values["SERVER_PUBLIC"] == "1"
    assert values["CROSSPLAY"] == "true"
    # Untouched keys survived.
    assert values["BACKUPS_INTERVAL"] == "3600"


def test_the_wizard_never_starts_the_server(fresh):
    """No world exists until the operator presses Start."""
    with TestClient(fresh["app"]) as client:
        assert complete_setup(client, fresh["token"]).status_code == 303
    docker = fresh["docker"]
    assert docker.containers.create_calls == []
    assert docker.containers.registry == {}
    assert docker.images.pulls == []


def test_unticked_checkboxes_mean_private_and_no_crossplay(fresh):
    with TestClient(fresh["app"]) as client:
        data = {**SETUP_FORM, "token": fresh["token"]}
        data.pop("server_public")
        data.pop("crossplay")
        assert client.post(
            "/setup", data=data, headers={"Origin": ORIGIN}, follow_redirects=False
        ).status_code == 303
    values = SettingsStore(fresh["config"].env_file).read()
    assert values["SERVER_PUBLIC"] == "0"
    assert values["CROSSPLAY"] == "false"


def test_setup_is_closed_permanently_once_completed(fresh):
    with TestClient(fresh["app"]) as client:
        assert complete_setup(client, fresh["token"]).status_code == 303
        before = open(fresh["config"].state_file, "rb").read()
        settings_before = open(fresh["config"].env_file, "rb").read()

        # The original token, reused: refused, and nothing overwritten.
        reopened = client.get(
            "/setup", params={"token": fresh["token"]}, follow_redirects=False
        )
        resubmitted = complete_setup(
            client, fresh["token"], admin_user="loki", password="another-password-x",
            password_confirm="another-password-x",
        )

    assert reopened.status_code == 302
    assert reopened.headers["location"] == "/login"
    assert resubmitted.status_code == 302
    assert resubmitted.headers["location"] == "/login"
    assert open(fresh["config"].state_file, "rb").read() == before
    assert open(fresh["config"].env_file, "rb").read() == settings_before


def test_setup_requires_a_same_origin_post(fresh):
    with TestClient(fresh["app"]) as client:
        response = client.post(
            "/setup",
            data={**SETUP_FORM, "token": fresh["token"]},
            headers={"Origin": "http://evil.example"},
        )
    assert response.status_code == 403
    assert not os.path.exists(fresh["config"].state_file)


def test_setup_never_logs_the_password_or_the_secret(fresh, caplog):
    with caplog.at_level(logging.DEBUG):
        with TestClient(fresh["app"]) as client:
            assert complete_setup(client, fresh["token"]).status_code == 303
    document = json.loads(open(fresh["config"].state_file, encoding="utf-8").read())
    assert "a-long-enough-password" not in caplog.text
    assert document["admin_password_hash"] not in caplog.text
    assert document["session_secret"] not in caplog.text


# -------------------------------------------------------------- field validation


@pytest.mark.parametrize(
    "overrides,needle",
    [
        ({"admin_user": "  "}, "admin username"),
        ({"admin_user": "od in"}, "spaces"),
        ({"password_confirm": "something-else-entirely"}, "do not match"),
        ({"password": "short", "password_confirm": "short"}, "at least"),
        ({"server_name": ""}, "name"),
        ({"world_name": "worlds/Dedicated"}, "slash"),
        ({"server_port": "70000"}, "65533"),
        ({"server_port": "not-a-port"}, "must be a number"),
        ({"server_pass": "abc"}, "at least 5"),
        ({"server_name": "Midgard hunter22 realm"}, "inside the server name"),
    ],
)
def test_invalid_setup_fields_are_refused_without_writing_anything(
    fresh, overrides, needle
):
    with TestClient(fresh["app"]) as client:
        response = complete_setup(client, fresh["token"], **overrides)
        assert response.status_code == 400
        assert needle in response.text
        # Still open on the same token, so the operator just fixes the field.
        assert client.get("/setup", params={"token": fresh["token"]}).status_code == 200
    assert not os.path.exists(fresh["config"].state_file)
    # The default file boot wrote is untouched.
    assert open(fresh["config"].env_file, encoding="utf-8").read() == DEFAULT_SETTINGS_TEXT


def test_a_rejected_form_comes_back_with_the_other_fields_still_filled_in(fresh):
    """Blanking the join password because the port was wrong would silently write an
    empty one on the retry."""
    with TestClient(fresh["app"]) as client:
        response = complete_setup(client, fresh["token"], server_port="70000")

    assert response.status_code == 400
    for kept in ("odin", "Midgard", "Yggdrasil", "hunter22"):
        assert kept in response.text
    # But never the admin password.
    assert "a-long-enough-password" not in response.text


def test_a_password_over_72_bytes_is_refused_with_argon2_offered(fresh):
    long_password = "A" * 80
    with TestClient(fresh["app"]) as client:
        response = complete_setup(
            client,
            fresh["token"],
            password=long_password,
            password_confirm=long_password,
        )
        assert response.status_code == 400
        assert "72" in response.text
        assert "argon2" in response.text

        # And argon2 takes the very same passphrase.
        accepted = complete_setup(
            client,
            fresh["token"],
            password=long_password,
            password_confirm=long_password,
            hash_algo="argon2",
        )
    assert accepted.status_code == 303
    document = json.loads(open(fresh["config"].state_file, encoding="utf-8").read())
    assert validate_password_hash(document["admin_password_hash"]) == "argon2"
    assert verify_password(long_password, document["admin_password_hash"])


# --------------------------------------------------- credentials already configured


def test_env_credentials_skip_setup_entirely(stack, caplog):
    """The previous build's behaviour, unchanged: no token, no wizard."""
    assert stack["app"].state.setup is None
    with TestClient(stack["app"]) as client:
        assert client.get("/setup", follow_redirects=False).status_code == 302
        assert client.get("/setup", follow_redirects=False).headers["location"] == "/login"
        assert client.get("/login").status_code == 200
        login(client)
        assert client.get("/", follow_redirects=False).status_code == 200
    assert "FIRST-RUN SETUP" not in caplog.text


def test_a_bad_env_hash_still_fails_loudly_at_boot(env_file, fake_docker):
    config = build_config(env_file, admin_password_hash="$2b$12")
    with pytest.raises(AuthConfigError) as excinfo:
        create_app(
            config=config,
            controller=build_control(config, fake_docker),
            settings=SettingsStore(env_file),
            state_store=StateStore(env_file.parent / "state.json"),
        )
    assert "Compose" in str(excinfo.value)


@pytest.mark.parametrize(
    "missing", ["ADMIN_USER", "ADMIN_PASSWORD_HASH", "SESSION_SECRET"]
)
def test_partially_configured_env_refuses_to_boot_instead_of_opening_setup(
    tmp_path, fake_docker, missing
):
    """Silently ignoring two of the three would hand the operator an admin-creation
    page and no clue why their credentials did nothing."""
    supplied = {
        "admin_user": ADMIN_USER,
        "admin_password_hash": ADMIN_HASH,
        "session_secret": SESSION_SECRET,
    }
    supplied[missing.lower()] = ""
    blank = blank_config(tmp_path)
    config = build_config(blank.env_file, state_file=blank.state_file, **supplied)
    with pytest.raises(ConfigError) as excinfo:
        create_app(
            config=config,
            controller=build_control(config, fake_docker),
            settings=SettingsStore(config.env_file),
            state_store=StateStore(config.state_file),
        )
    assert missing in str(excinfo.value)


def test_credentials_load_from_the_state_file_after_a_restart(tmp_path, fake_docker):
    bundle = unconfigured(tmp_path, fake_docker)
    with TestClient(bundle["app"]) as client:
        complete_setup(client, bundle["token"])
        cookie = client.cookies["valheim_manager_session"]

    # A fresh app object is a restarted (or recreated) manager container.
    restarted = create_app(
        config=blank_config(tmp_path),
        controller=build_control(bundle["config"], fake_docker),
        settings=SettingsStore(bundle["config"].env_file),
        state_store=StateStore(bundle["config"].state_file),
    )
    assert restarted.state.setup is None
    with TestClient(restarted) as client:
        client.cookies.set("valheim_manager_session", cookie)
        # The session survives, because the secret came back off the state file.
        assert client.get("/", follow_redirects=False).status_code == 200
        # And setup stays closed.
        assert client.get("/setup", follow_redirects=False).headers["location"] == "/login"
        # The credentials still work for a fresh sign-in too.
        client.cookies.clear()
        assert client.post(
            "/login",
            data={"username": "odin", "password": "a-long-enough-password"},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        ).status_code == 303


# ------------------------------------------------------------ the state file itself


def test_state_file_round_trips(tmp_path):
    path = tmp_path / "state" / "manager-state.json"
    store = StateStore(path)
    state = ManagerState(
        admin_user="odin",
        admin_password_hash=ADMIN_HASH,
        session_secret=new_session_secret(),
    )

    store.save(state)

    assert store.load() == state
    # Nothing left behind by the temp-then-replace write.
    assert list(path.parent.glob(".state-*")) == []


@POSIX_ONLY
def test_state_file_is_mode_0600(tmp_path):
    path = tmp_path / "state" / "manager-state.json"
    StateStore(path).save(
        ManagerState(
            admin_user="odin",
            admin_password_hash=ADMIN_HASH,
            session_secret=SESSION_SECRET,
        )
    )
    assert stat.S_IMODE(path.stat().st_mode) == STATE_MODE
    assert STATE_MODE == 0o600


def test_state_file_mode_is_set_before_the_rename(tmp_path, monkeypatch):
    """Runs everywhere, including where the filesystem ignores chmod: the secrets must
    never be world-readable, not even for the instant between write and rename."""
    import app.state_store as state_store_module

    calls: list[tuple[str, int]] = []
    real_chmod = os.chmod

    def recording_chmod(target, mode, *args, **kwargs):
        calls.append((str(target), mode))
        return real_chmod(target, mode, *args, **kwargs)

    monkeypatch.setattr(state_store_module.os, "chmod", recording_chmod)
    path = tmp_path / "state" / "manager-state.json"
    StateStore(path).save(
        ManagerState(
            admin_user="odin",
            admin_password_hash=ADMIN_HASH,
            session_secret=SESSION_SECRET,
        )
    )

    assert calls, "the state file was written without setting its mode"
    assert all(mode == STATE_MODE for _, mode in calls)
    # The first chmod is on the temp file, i.e. before os.replace.
    assert ".state-" in calls[0][0]


def test_created_at_survives_a_re_save(tmp_path):
    path = tmp_path / "state" / "manager-state.json"
    store = StateStore(path)
    state = ManagerState(
        admin_user="odin", admin_password_hash=ADMIN_HASH, session_secret=SESSION_SECRET
    )
    store.save(state)
    first = json.loads(path.read_text(encoding="utf-8"))["created_at"]

    store.save(ManagerState(admin_user="thor", admin_password_hash=ADMIN_HASH,
                            session_secret=SESSION_SECRET))

    assert json.loads(path.read_text(encoding="utf-8"))["created_at"] == first


@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        "[]",
        '{"admin_user": "odin"}',
        '{"admin_user": "odin", "admin_password_hash": "", "session_secret": "s"}',
        '{"version": 99, "admin_user": "o", "admin_password_hash": "'
        + ADMIN_HASH
        + '", "session_secret": "' + SESSION_SECRET + '"}',
    ],
)
def test_a_corrupt_state_file_refuses_to_boot_and_names_itself(
    tmp_path, fake_docker, content
):
    """Falling back to "unconfigured" here would reopen admin creation on a manager
    that already has an admin -- the one failure mode that must never happen."""
    config = blank_config(tmp_path)
    state_path = tmp_path / "state" / "manager-state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(content, encoding="utf-8")

    with pytest.raises(StateStoreError) as excinfo:
        create_app(
            config=config,
            controller=build_control(config, fake_docker),
            settings=SettingsStore(config.env_file),
            state_store=StateStore(state_path),
        )

    assert str(state_path) in str(excinfo.value)


def test_a_state_file_holding_an_unusable_hash_names_the_file_not_manager_env(
    tmp_path, fake_docker
):
    config = blank_config(tmp_path)
    state_path = tmp_path / "state" / "manager-state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "admin_user": "odin",
                "admin_password_hash": "$2b$12",  # Compose-style wreckage
                "session_secret": SESSION_SECRET,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StateStoreError) as excinfo:
        create_app(
            config=config,
            controller=build_control(config, fake_docker),
            settings=SettingsStore(config.env_file),
            state_store=StateStore(state_path),
        )
    message = str(excinfo.value)
    assert str(state_path) in message
    # And it must not reopen setup by pretending nothing was configured.
    assert "delete it to run first-run setup again" in message


def test_an_unusable_state_directory_refuses_to_boot_naming_the_path(tmp_path):
    """An operator who has just run `docker compose up -d` should learn the volume is
    unusable from the first log line, not after typing a password into a form."""
    blocked = tmp_path / "state"
    blocked.write_text("not a directory", encoding="utf-8")

    with pytest.raises(StateStoreError) as excinfo:
        StateStore(blocked / "manager-state.json").ensure_writable()

    assert str(blocked) in str(excinfo.value)
    assert "valheim-manager-state" in str(excinfo.value)


def test_a_state_path_that_is_a_directory_is_named_as_such(tmp_path):
    as_dir = tmp_path / "manager-state.json"
    as_dir.mkdir()
    with pytest.raises(StateStoreError) as excinfo:
        StateStore(as_dir).load()
    assert "directory" in str(excinfo.value)


def test_generated_session_secrets_are_long_and_unique():
    secrets_seen = {new_session_secret() for _ in range(20)}
    assert len(secrets_seen) == 20
    # SessionAuth refuses anything under 16 characters.
    assert all(len(secret) >= 16 for secret in secrets_seen)


# -------------------------------------------- the atomic, comment-preserving write


OPERATOR_SETTINGS = """\
# My own header, with a $ in it.
SERVER_NAME="Old Name"   # inline note
export SERVER_PORT = 2456

# A key the manager knows nothing about.
MY_OWN_KEY=keep-me
SERVER_PASS='old-pass'
"""


def test_write_preserves_comments_order_spacing_and_unknown_keys(tmp_path):
    path = tmp_path / "valheim.env"
    path.write_text(OPERATOR_SETTINGS, encoding="utf-8")

    SettingsStore(path).write({"SERVER_NAME": "New Name", "SERVER_PASS": "new-pass"})

    text = path.read_text(encoding="utf-8")
    assert "# My own header, with a $ in it." in text
    assert "MY_OWN_KEY=keep-me" in text
    # Ordering and the `export ` prefix and spacing are untouched.
    assert text.index("SERVER_NAME") < text.index("SERVER_PORT") < text.index("MY_OWN_KEY")
    assert "export SERVER_PORT = 2456" in text
    # The inline comment on a rewritten line survives.
    assert "# inline note" in text
    # And the new values read back exactly.
    values = parse_env_text(text)
    assert values["SERVER_NAME"] == "New Name"
    assert values["SERVER_PASS"] == "new-pass"
    assert values["MY_OWN_KEY"] == "keep-me"
    assert values["SERVER_PORT"] == "2456"


def test_write_appends_a_key_the_file_does_not_have(tmp_path):
    path = tmp_path / "valheim.env"
    path.write_text("SERVER_NAME=Old\n", encoding="utf-8")

    SettingsStore(path).write({"SERVER_NAME": "New", "CROSSPLAY": "true"})

    values = parse_env_text(path.read_text(encoding="utf-8"))
    assert values == {"SERVER_NAME": "New", "CROSSPLAY": "true"}


@pytest.mark.parametrize(
    "line,expected",
    [
        ('NAME="My Server" # note', "My Server"),
        ("NAME='My Server'   # note", "My Server"),
        ('NAME="My Server"', "My Server"),
        ("NAME=plain   # note", "plain"),
        ("NAME=has#hash", "has#hash"),
        ("NAME=", ""),
        ('NAME=""   # note', ""),
    ],
)
def test_a_quoted_value_with_an_inline_comment_loses_both(line, expected):
    """Regression: deciding "is it quoted?" from the *whole* right-hand side left the
    quotes in the value whenever an inline comment followed, so a value the writer had
    to quote did not survive a round trip."""
    assert parse_env_text(line + "\n")["NAME"] == expected


@pytest.mark.parametrize(
    "value", ["New Name", "has a # hash", "it's", 'a "quoted" word', "", "plain"]
)
def test_every_written_value_reads_back_unchanged_next_to_a_comment(tmp_path, value):
    """format_env_value's contract, on the line shape that used to break it."""
    path = tmp_path / "valheim.env"
    path.write_text("SERVER_NAME=old   # keep me\n", encoding="utf-8")

    SettingsStore(path).write({"SERVER_NAME": value})

    text = path.read_text(encoding="utf-8")
    assert "# keep me" in text
    assert parse_env_text(text)["SERVER_NAME"] == value


def test_values_needing_quotes_round_trip(tmp_path):
    path = tmp_path / "valheim.env"
    path.write_text("SERVER_NAME=Old\nSERVER_PASS=\n", encoding="utf-8")

    SettingsStore(path).write(
        {"SERVER_NAME": "Two  Spaces # and a hash", "SERVER_PASS": "it's quoted"}
    )

    values = parse_env_text(path.read_text(encoding="utf-8"))
    assert values["SERVER_NAME"] == "Two  Spaces # and a hash"
    assert values["SERVER_PASS"] == "it's quoted"


def test_a_failed_write_leaves_the_original_file_intact(tmp_path, monkeypatch):
    """Temp-then-replace exists so the valheim service's env_file never reads a
    half-written file."""
    import app.settings_store as settings_module

    path = tmp_path / "valheim.env"
    path.write_text(OPERATOR_SETTINGS, encoding="utf-8")
    monkeypatch.setattr(
        settings_module.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    )

    with pytest.raises(SettingsFileError):
        SettingsStore(path).write({"SERVER_NAME": "New Name"})

    assert path.read_text(encoding="utf-8") == OPERATOR_SETTINGS
    assert list(tmp_path.glob(".valheim-env-*")) == []


def test_render_env_text_never_loses_a_line(tmp_path):
    rendered = render_env_text(OPERATOR_SETTINGS, {"SERVER_NAME": "New"})
    assert len(rendered.splitlines()) == len(OPERATOR_SETTINGS.splitlines())


def test_an_operator_settings_file_is_left_byte_identical_by_boot(tmp_path, fake_docker):
    """The matrix's strongest promise: a file present before first boot is not
    overwritten and not reformatted."""
    settings_path = tmp_path / "settings" / "valheim.env"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_bytes(OPERATOR_SETTINGS.encode("utf-8"))
    before = settings_path.read_bytes()

    config = blank_config(tmp_path)
    create_app(
        config=config,
        controller=build_control(config, fake_docker),
        settings=SettingsStore(settings_path),
        state_store=StateStore(config.state_file),
    )

    assert settings_path.read_bytes() == before


def test_ensure_default_reports_whether_it_created_the_file(tmp_path):
    path = tmp_path / "settings" / "valheim.env"
    store = SettingsStore(path)
    assert store.ensure_default() is True
    assert store.ensure_default() is False
    assert path.read_text(encoding="utf-8") == DEFAULT_SETTINGS_TEXT


def test_default_settings_text_matches_the_shipped_example(project_root):
    """The example file is the documented, hand-edited path; the text the manager
    writes is compiled into the image. They must not drift apart."""
    example = parse_env_text(
        (project_root / "valheim.env.example").read_text(encoding="utf-8")
    )
    assert parse_env_text(DEFAULT_SETTINGS_TEXT) == example


def test_the_default_settings_file_cannot_start_a_public_server():
    values = parse_env_text(DEFAULT_SETTINGS_TEXT)
    assert values["SERVER_PASS"] == ""
    assert values["SERVER_PUBLIC"] in ("0", "false", "False")


def test_settings_panel_says_how_a_saved_change_takes_effect(stack):
    """Saving alone changes nothing the running server can see, so the panel has to say
    what makes a saved value real. It says it in the operator's terms -- turn it off,
    save, press Start -- rather than explaining containers."""
    with TestClient(stack["app"]) as client:
        login(client)
        words = " ".join(client.get("/").text.split())
    assert "Turn the server off to change them" in words
    assert "and the new settings take effect" in words
    assert "Your world and your backups are never touched" in words


# =====================================================================
# Review round three: the join password, encodings, and the surfaces where
# deleting real behaviour used to leave the suite green.
# =====================================================================

# ----------------------------------- the join password the server insists on


@pytest.mark.parametrize("join_password", ["", "abc", "four"])
def test_the_wizard_refuses_a_join_password_the_server_would_reject(fresh, join_password):
    """`valheim_server.x86_64` refuses to start without 5+ characters, whatever
    SERVER_PUBLIC says. Accepting one here means the operator follows the README to
    the letter, presses Start, and gets a container that exits on its own."""
    with TestClient(fresh["app"]) as client:
        response = complete_setup(client, fresh["token"], server_pass=join_password)

    assert response.status_code == 400
    assert "at least 5" in response.text
    assert not os.path.exists(fresh["config"].state_file)


def test_the_setup_page_does_not_offer_an_empty_join_password(fresh):
    with TestClient(fresh["app"]) as client:
        body = client.get("/setup", params={"token": fresh["token"]}).text
    assert "Leave it empty" not in body
    assert 'name="server_pass"' in body
    assert "required" in body


@pytest.mark.parametrize("join_password", ["", "abc"])
def test_start_refuses_and_names_the_cause_when_the_join_password_is_unusable(
    tmp_path, fake_docker, join_password
):
    """The settings file is the operator's to edit after setup. Left to Docker this
    shows up as "stopped / Container exited" with no cause anywhere in the UI."""
    path = tmp_path / "valheim.env"
    path.write_text(f"SERVER_NAME=Midgard\nSERVER_PASS={join_password}\n", encoding="utf-8")
    config = build_config(path)
    app = create_app(
        config=config,
        controller=build_control(config, fake_docker),
        settings=SettingsStore(path),
    )

    with TestClient(app) as client:
        login(client)
        started = client.post("/api/start", headers={"Origin": ORIGIN}, json={})
        restarted = client.post("/api/restart", headers={"Origin": ORIGIN}, json={})

    for response in (started, restarted):
        assert response.status_code == 400
        assert "SERVER_PASS" in response.json()["error"]
        assert "exit immediately" in response.json()["error"]
    # And it really did refuse before touching Docker.
    assert fake_docker.containers.create_calls == []


def test_start_still_works_with_a_usable_join_password(stack):
    with TestClient(stack["app"]) as client:
        login(client)
        response = client.post("/api/start", headers={"Origin": ORIGIN}, json={})
    assert response.status_code == 200


# ------------------------------------------------- a settings file that is not UTF-8


@pytest.mark.parametrize(
    "raw",
    [
        # A single accented character saved by a legacy editor.
        "SERVER_NAME=Café\n".encode("latin-1"),
        # What Notepad calls "Unicode".
        "SERVER_NAME=Midgard\n".encode("utf-16"),
    ],
)
def test_a_settings_file_that_is_not_utf8_is_named_not_a_500(tmp_path, fake_docker, raw):
    """This is the one file the design invites the operator to edit on the host, and
    that host is often Windows. A raw UnicodeDecodeError 500s the dashboard and kills
    the log pump."""
    path = tmp_path / "valheim.env"
    path.write_bytes(raw)
    store = SettingsStore(path)

    with pytest.raises(SettingsFileError) as read_error:
        store.read()
    with pytest.raises(SettingsFileError) as write_error:
        store.write({"SERVER_NAME": "New"})

    for excinfo in (read_error, write_error):
        assert "UTF-8" in str(excinfo.value)
        assert str(path) in str(excinfo.value)

    # And the UI reports it instead of falling over.
    config = build_config(path)
    app = create_app(
        config=config, controller=build_control(config, fake_docker), settings=store
    )
    with TestClient(app) as client:
        login(client)
        payload = client.get("/api/status").json()
    assert payload["settings"] == []
    assert "UTF-8" in payload["settings_error"]


# --------------------------------------------------------- force stop is a boolean


@pytest.mark.parametrize("body", [{"force": "false"}, {"force": "no"}, {"force": 0}, {}])
def test_only_a_real_boolean_true_forces_a_kill(stack, body):
    """`{"force": "false"}` is truthy in Python. Reading it as force SIGKILLs a
    running server -- losing the world since the last autosave -- when the caller
    plainly asked for the opposite."""
    docker = stack["docker"]
    container = docker.seed_running()
    with TestClient(stack["app"]) as client:
        login(client)
        response = client.post("/api/stop", headers={"Origin": ORIGIN}, json=body)
    assert response.status_code == 200
    assert container.kill_calls == 0
    assert container.stop_calls == [7]


# ------------------------------------- (a) the app's own controller, not the fixture's


def test_the_app_builds_its_own_controller_with_the_full_udp_port_range(env_file, tmp_path):
    """Every other test injects a controller, so the production wiring -- including
    the port math -- was never executed. Dropping the query and crossplay ports breaks
    the Steam server browser and crossplay joins while the suite stays green."""
    config = build_config(env_file, state_file=str(tmp_path / "state.json"))
    app = create_app(config=config, settings=SettingsStore(env_file))

    control = app.state.control
    assert control.port_provider() == [2456, 2457, 2458]
    assert control.container_name == CONTAINER
    assert control.image == IMAGE
    assert control.env_provider()["SERVER_NAME"] == "Midgard Test"


def test_udp_ports_covers_game_query_and_crossplay(tmp_path):
    from app.main import _udp_ports

    path = tmp_path / "valheim.env"
    path.write_text("SERVER_PORT=2500\n", encoding="utf-8")
    assert _udp_ports(SettingsStore(path)) == [2500, 2501, 2502]


# ------------------------------- (b) config_from_env's non-numeric reads, via the env


def test_allowed_origins_is_read_and_split_from_the_environment(monkeypatch):
    """A rename here silently 403s every reverse-proxy operator on Start and Stop."""
    from app.main import config_from_env

    monkeypatch.setenv("ALLOWED_ORIGINS", " https://valheim.lan , https://a.example ,,")
    assert config_from_env().allowed_origins == [
        "https://valheim.lan",
        "https://a.example",
    ]

    monkeypatch.delenv("ALLOWED_ORIGINS")
    assert config_from_env().allowed_origins == []


def test_the_credential_trio_is_read_from_the_environment(monkeypatch):
    from app.main import config_from_env

    monkeypatch.setenv("ADMIN_USER", ADMIN_USER)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", ADMIN_HASH)
    monkeypatch.setenv("SESSION_SECRET", SESSION_SECRET)
    config = config_from_env()
    assert config.admin_user == ADMIN_USER
    assert config.admin_password_hash == ADMIN_HASH
    assert config.session_secret == SESSION_SECRET


@pytest.mark.parametrize(
    "value,expected",
    [("true", True), ("1", True), ("on", True), ("false", False), ("0", False), ("", False)],
)
def test_cookie_secure_is_read_from_the_environment(monkeypatch, value, expected):
    from app.main import config_from_env

    monkeypatch.setenv("COOKIE_SECURE", value)
    assert config_from_env().cookie_secure is expected


def test_manager_url_and_paths_are_read_from_the_environment(monkeypatch):
    from app.main import config_from_env

    monkeypatch.setenv("MANAGER_URL", "  http://valheim.lan:8080  ")
    monkeypatch.setenv("VALHEIM_ENV_FILE", "/srv/settings/valheim.env")
    monkeypatch.setenv("MANAGER_STATE_FILE", "/srv/state/manager-state.json")
    config = config_from_env()
    assert config.manager_url == "http://valheim.lan:8080"
    assert config.env_file == "/srv/settings/valheim.env"
    assert config.state_file == "/srv/state/manager-state.json"


def test_every_knob_the_manager_reads_is_passed_through_by_compose(project_root):
    """STATUS_EVERY_N_POLLS was documented and read but never passed through, so
    setting it in .env silently did nothing. A test that monkeypatches the
    environment cannot see that; only the compose file can."""
    compose = (project_root / "docker-compose.yml").read_text(encoding="utf-8")
    manager_block = compose.split("  manager:", 1)[1]
    for name in (
        "SESSION_MAX_AGE_SECONDS",
        "COOKIE_SECURE",
        "ALLOWED_ORIGINS",
        "VALHEIM_CONTAINER_NAME",
        "VALHEIM_IMAGE",
        "VALHEIM_RESTART_POLICY",
        "VALHEIM_STOP_TIMEOUT",
        "VALHEIM_ENV_FILE",
        "MANAGER_STATE_FILE",
        "MANAGER_URL",
        "READY_LOG_PATTERN",
        "LOG_TAIL_LINES",
        "LOG_POLL_SECONDS",
        "STATUS_EVERY_N_POLLS",
    ):
        assert f"{name}:" in manager_block, f"{name} is never passed to the manager"


# ------------------------------------------- (c) a failure inside commit(), not prepare


def test_a_settings_write_failure_leaves_setup_open_and_writes_no_credentials(fresh):
    """The whole point of splitting prepare from commit: this is the mount failure the
    split exists to handle, and swapping the two writes would ship a half-completed
    install -- an admin account whose settings were never written, with the wizard
    already closed."""

    def refuse(_updates):
        raise SettingsFileError("The settings directory /srv/settings is not writable.")

    fresh["settings"].write = refuse

    with TestClient(fresh["app"]) as client:
        response = complete_setup(client, fresh["token"])
        assert response.status_code == 500
        assert "not writable" in response.text
        # Still open, on the same token, so fixing the mount is all it takes.
        assert client.get("/setup", params={"token": fresh["token"]}).status_code == 200

    assert fresh["app"].state.auth is None
    assert fresh["app"].state.setup.is_open() is True
    assert not os.path.exists(fresh["config"].state_file)


# ------------------------------------------------- (d) the settings file's mode


def test_write_preserves_the_existing_file_mode(tmp_path, monkeypatch):
    """Dropping the chmod silently leaves the operator's file at the temp file's 0600,
    which breaks host editing and the `--profile server` path that reads it."""
    import app.settings_store as settings_module

    path = tmp_path / "valheim.env"
    path.write_text("SERVER_NAME=Old\n", encoding="utf-8")
    original = stat.S_IMODE(path.stat().st_mode)

    calls: list[tuple[str, int]] = []
    real_chmod = os.chmod

    def recording_chmod(target, mode, *args, **kwargs):
        calls.append((str(target), mode))
        return real_chmod(target, mode, *args, **kwargs)

    monkeypatch.setattr(settings_module.os, "chmod", recording_chmod)
    SettingsStore(path).write({"SERVER_NAME": "New"})

    assert calls, "the settings file was written without restoring its mode"
    assert all(mode == original for _, mode in calls)
    # On the temp file, i.e. before os.replace -- never a window at the wrong mode.
    assert ".valheim-env-" in calls[0][0]


@POSIX_ONLY
def test_a_group_readable_settings_file_stays_group_readable(tmp_path):
    path = tmp_path / "valheim.env"
    path.write_text("SERVER_NAME=Old\n", encoding="utf-8")
    os.chmod(path, 0o640)

    SettingsStore(path).write({"SERVER_NAME": "New"})

    assert stat.S_IMODE(path.stat().st_mode) == 0o640


# --------------------------------------------------- (e) /login's own origin check


def test_login_rejects_a_foreign_origin(stack):
    with TestClient(stack["app"]) as client:
        response = client.post(
            "/login",
            data={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
            headers={"Origin": "http://evil.example"},
            follow_redirects=False,
        )
    assert response.status_code == 403
    assert "cross-site" in response.json()["error"]
    assert "valheim_manager_session" not in response.headers.get("set-cookie", "")


def test_login_rejects_a_request_with_no_origin(stack):
    with TestClient(stack["app"]) as client:
        response = client.post(
            "/login",
            data={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
            follow_redirects=False,
        )
    assert response.status_code == 403
    assert "missing Origin" in response.json()["error"]
    assert "valheim_manager_session" not in response.headers.get("set-cookie", "")


# -------------------------------------------- duplicate log lines in a one-second burst


def test_a_burst_of_lines_in_one_second_is_never_re_sent(stack):
    """`since` has whole-second resolution, so every poll re-reads its final second.
    A fixed-size seen-set evicts exactly the entries that re-read needs to match once
    SteamCMD emits more lines in a second than the set holds -- duplicates, at the
    moment the README tells the operator to watch the console."""
    docker = stack["docker"]
    container = docker.seed_running()
    container.log_epoch_step = 0  # the whole burst inside one second
    container.log_messages = [f"steamcmd progress {i:05d}" for i in range(2500)]

    seen: list[str] = []
    with TestClient(stack["app"]) as client:
        login(client)
        with client.websocket_connect("/ws/logs") as ws:
            for _ in range(120):
                message = ws.receive_json()
                if message["type"] == "log":
                    seen.extend(message["lines"])

    assert seen, "no log lines arrived at all"
    assert len(seen) == len(set(seen)), "the console was sent duplicate lines"


# ------------------------------------------------ the editable settings panel
#
# The whole point of the panel is the container removal: `start()` creates a container
# only when none exists, so a save that wrote the file and left the stopped container
# in place would be a save the next Start silently ignores.


def save_settings(client: TestClient, settings: dict, *, origin: str = ORIGIN):
    return client.post(
        "/api/settings", json={"settings": settings}, headers={"Origin": origin}
    )


def test_editing_a_value_writes_the_file_and_removes_the_stopped_container(stack, env_file):
    docker = stack["docker"]
    container = docker.seed_stopped()

    with TestClient(stack["app"]) as client:
        login(client)
        response = save_settings(
            client,
            {
                "SERVER_NAME": "Asgard",
                "WORLD_NAME": "Dedicated",
                "SERVER_PASS": MASK,
                "SERVER_PORT": "2470",
                "SERVER_PUBLIC": "1",
                "CROSSPLAY": "false",
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["saved"] is True
    assert payload["changed"] == ["SERVER_NAME", "SERVER_PORT"]
    assert payload["container_removed"] is True
    assert container.removed is True
    assert payload["status"]["container_exists"] is False
    # The panel shows the new values straight away, still masked.
    rows = {row["key"]: row["value"] for row in payload["settings"]}
    assert rows["SERVER_NAME"] == "Asgard"
    assert rows["SERVER_PORT"] == "2470"
    assert rows["SERVER_PASS"] == MASK
    # ... and the world lives on volumes, which is why the message can say so. Both
    # of them, matching the panel, the README and remove_stopped_container itself.
    assert "Your world and your backups were not touched" in payload["message"]

    text = env_file.read_text(encoding="utf-8")
    assert "SERVER_NAME=Asgard" in text
    assert "SERVER_PORT=2470" in text
    # Comments, blank lines, `export `, the inline comment and unknown keys: untouched.
    assert "# Valheim settings" in text
    assert "TZ=Etc/UTC   # inline comment" in text
    assert "export SERVER_PUBLIC=1" in text
    assert "SERVER_PASS='hunter22'" in text
    assert MASK not in text


def test_the_next_start_recreates_the_container_with_the_saved_values(stack, env_file):
    docker = stack["docker"]
    docker.seed_stopped()

    with TestClient(stack["app"]) as client:
        login(client)
        assert save_settings(client, {"SERVER_NAME": "Asgard"}).status_code == 200
        assert client.post("/api/start", headers={"Origin": ORIGIN}).status_code == 200

    # A second create, because the first container was removed by the save.
    assert len(docker.containers.create_calls) == 2
    assert docker.containers.create_calls[-1]["environment"]["SERVER_NAME"] == "Asgard"


def test_saving_with_no_container_at_all_still_saves(stack, env_file):
    with TestClient(stack["app"]) as client:
        login(client)
        response = save_settings(client, {"SERVER_NAME": "Asgard"})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["saved"] is True
    assert payload["container_removed"] is False
    assert "SERVER_NAME=Asgard" in env_file.read_text(encoding="utf-8")


def test_a_save_that_changes_nothing_writes_nothing_and_keeps_the_container(stack, env_file):
    docker = stack["docker"]
    container = docker.seed_stopped()
    before = env_file.read_text(encoding="utf-8")

    with TestClient(stack["app"]) as client:
        login(client)
        response = save_settings(
            client,
            {
                "SERVER_NAME": "Midgard Test",
                "WORLD_NAME": "Dedicated",
                "SERVER_PASS": MASK,
                "SERVER_PORT": "2456",
                "SERVER_PUBLIC": "1",
                "CROSSPLAY": "false",
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["changed"] == []
    assert payload["container_removed"] is False
    assert container.removed is False
    assert env_file.read_text(encoding="utf-8") == before


def test_an_untouched_masked_secret_keeps_the_stored_join_password(stack, env_file):
    docker = stack["docker"]
    docker.seed_stopped()

    with TestClient(stack["app"]) as client:
        login(client)
        response = save_settings(client, {"SERVER_NAME": "Asgard", "SERVER_PASS": MASK})

    assert response.status_code == 200, response.text
    values = parse_env_text(env_file.read_text(encoding="utf-8"))
    assert values["SERVER_PASS"] == "hunter22"
    assert values["SERVER_NAME"] == "Asgard"


def test_a_retyped_join_password_is_stored_and_never_logged(stack, env_file, caplog):
    docker = stack["docker"]
    docker.seed_stopped()

    with caplog.at_level(logging.DEBUG), TestClient(stack["app"]) as client:
        login(client)
        response = save_settings(client, {"SERVER_PASS": "thundercloud"})

    assert response.status_code == 200, response.text
    assert parse_env_text(env_file.read_text(encoding="utf-8"))["SERVER_PASS"] == "thundercloud"
    assert "thundercloud" not in caplog.text
    # The key is logged, so the operator can see *that* it changed.
    assert "SERVER_PASS" in caplog.text


def test_editing_is_refused_while_the_server_runs(stack, env_file):
    docker = stack["docker"]
    container = docker.seed_running()
    before = env_file.read_text(encoding="utf-8")

    with TestClient(stack["app"]) as client:
        login(client)
        response = save_settings(client, {"SERVER_NAME": "Asgard"})

    assert response.status_code == 409, response.text
    payload = response.json()
    assert "running" in payload["error"].lower()
    assert "Turn the server off first" in payload["error"]
    assert payload["saved"] is False
    assert env_file.read_text(encoding="utf-8") == before
    assert container.removed is False


@pytest.mark.parametrize("state", ["running", "restarting", "paused"])
def test_every_live_state_refuses_the_save(stack, env_file, state):
    docker = stack["docker"]
    container = docker.seed_stopped()
    container.status = state
    before = env_file.read_text(encoding="utf-8")

    with TestClient(stack["app"]) as client:
        login(client)
        response = save_settings(client, {"SERVER_NAME": "Asgard"})

    assert response.status_code == 409, response.text
    assert env_file.read_text(encoding="utf-8") == before
    assert container.removed is False


def test_a_server_started_after_the_editor_opened_still_refuses_the_save(stack, env_file):
    """The browser's phase is up to a poll interval old, and the server can be started
    from another tab or straight from the host in between."""
    docker = stack["docker"]
    container = docker.seed_stopped()
    before = env_file.read_text(encoding="utf-8")

    with TestClient(stack["app"]) as client:
        login(client)
        # What the editor opened on: the server was off.
        opened = client.get("/api/status").json()
        assert opened["status"]["phase"] == "stopped"

        container.status = "running"  # started elsewhere, editor still open
        response = save_settings(client, {"SERVER_NAME": "Asgard"})

    assert response.status_code == 409, response.text
    assert "running" in response.json()["error"].lower()
    # And the answer carries the truth, so the panel goes back to read-only at once.
    assert response.json()["status"]["phase"] in ("running", "ready")
    assert env_file.read_text(encoding="utf-8") == before
    assert container.removed is False


@pytest.mark.parametrize(
    "settings,expected",
    [
        ({"SERVER_PASS": "abcd"}, "at least 5 characters"),
        ({"SERVER_PORT": "70000"}, "between 1 and 65533"),
        ({"SERVER_PORT": "twenty"}, "must be a number"),
        ({"WORLD_NAME": "worlds/mine"}, "cannot contain a slash"),
        ({"SERVER_NAME": ""}, "Give the server a name."),
        ({"SERVER_PUBLIC": "maybe"}, "SERVER_PUBLIC must be 0 or 1."),
        ({"CROSSPLAY": "sometimes"}, "CROSSPLAY must be true or false."),
    ],
)
def test_an_invalid_value_names_the_field_and_writes_nothing(stack, env_file, settings, expected):
    docker = stack["docker"]
    container = docker.seed_stopped()
    before = env_file.read_text(encoding="utf-8")

    with TestClient(stack["app"]) as client:
        login(client)
        response = save_settings(client, settings)

    assert response.status_code == 400, response.text
    assert expected in response.json()["error"]
    assert env_file.read_text(encoding="utf-8") == before
    assert container.removed is False


def test_renaming_the_server_to_contain_the_stored_password_is_refused(stack, env_file):
    """A cross-field rule checked against what the file would end up holding: the
    password is not in this submission at all, but the pair still has to be legal."""
    docker = stack["docker"]
    docker.seed_stopped()
    before = env_file.read_text(encoding="utf-8")

    with TestClient(stack["app"]) as client:
        login(client)
        response = save_settings(client, {"SERVER_NAME": "join with hunter22"})

    assert response.status_code == 400, response.text
    assert "cannot appear inside the server name" in response.json()["error"]
    assert env_file.read_text(encoding="utf-8") == before


def test_a_value_the_env_file_cannot_hold_is_refused_before_the_write(stack, env_file):
    docker = stack["docker"]
    docker.seed_stopped()
    before = env_file.read_text(encoding="utf-8")

    with TestClient(stack["app"]) as client:
        login(client)
        response = save_settings(client, {"SERVER_NAME": "both \" and ' quotes"})

    assert response.status_code == 400, response.text
    assert "quote" in response.json()["error"]
    assert env_file.read_text(encoding="utf-8") == before


def test_an_already_empty_join_password_does_not_block_an_unrelated_edit(tmp_path, fake_docker):
    """Only what changed is validated. The stored SERVER_PASS is too short for the game
    server -- which Start refuses on its own -- and that must not be a reason the
    operator cannot fix the server's name."""
    env_file = tmp_path / "valheim.env"
    env_file.write_text("SERVER_NAME=Old\nSERVER_PASS=\n", encoding="utf-8")
    config = build_config(env_file)
    control = build_control(config, fake_docker)
    app = create_app(config=config, controller=control, settings=SettingsStore(env_file))
    fake_docker.seed_stopped()

    with TestClient(app) as client:
        login(client)
        response = save_settings(client, {"SERVER_NAME": "New", "SERVER_PASS": ""})

    assert response.status_code == 200, response.text
    values = parse_env_text(env_file.read_text(encoding="utf-8"))
    assert values == {"SERVER_NAME": "New", "SERVER_PASS": ""}


def test_a_removal_failure_says_the_file_was_written_and_names_the_docker_error(stack, env_file):
    docker = stack["docker"]
    container = docker.seed_stopped()

    def boom(force: bool = False):
        raise APIError("container removal in progress")

    container.remove = boom

    with TestClient(stack["app"]) as client:
        login(client)
        response = save_settings(client, {"SERVER_NAME": "Asgard"})

    assert response.status_code == 502, response.text
    payload = response.json()
    # The file IS written, so the operator is told what is still owed rather than being
    # left to believe nothing happened.
    assert payload["saved"] is True
    assert payload["container_removed"] is False
    assert "container removal in progress" in payload["docker_error"]
    assert "docker rm valheim-server" in payload["error"]
    assert "SERVER_NAME=Asgard" in env_file.read_text(encoding="utf-8")


def test_a_docker_that_cannot_be_reached_refuses_the_save_rather_than_writing(stack, env_file):
    docker = stack["docker"]
    docker.get_error = requests.exceptions.ConnectionError("proxy down")
    before = env_file.read_text(encoding="utf-8")

    with TestClient(stack["app"]) as client:
        login(client)
        response = save_settings(client, {"SERVER_NAME": "Asgard"})

    assert response.status_code == 502, response.text
    assert response.json()["saved"] is False
    assert "not saved" in response.json()["error"]
    assert env_file.read_text(encoding="utf-8") == before


def test_an_unreadable_settings_file_refuses_the_save_with_the_named_error(tmp_path, fake_docker):
    env_file = tmp_path / "valheim.env"
    env_file.write_bytes(b"SERVER_NAME=caf\xe9\n")  # latin-1, not UTF-8
    config = build_config(env_file)
    control = build_control(config, fake_docker)
    app = create_app(config=config, controller=control, settings=SettingsStore(env_file))
    fake_docker.seed_stopped()

    with TestClient(app) as client:
        login(client)
        response = save_settings(client, {"SERVER_NAME": "Asgard"})

    # 500, not 400: a file the manager cannot read is a fault on this side, and the
    # write failure answers the same way.
    assert response.status_code == 500, response.text
    assert "not valid UTF-8" in response.json()["error"]
    assert env_file.read_bytes() == b"SERVER_NAME=caf\xe9\n"


def test_saving_needs_a_session_and_a_same_origin_post(stack, env_file):
    docker = stack["docker"]
    docker.seed_stopped()
    before = env_file.read_text(encoding="utf-8")

    with TestClient(stack["app"]) as client:
        anonymous = save_settings(client, {"SERVER_NAME": "Asgard"})
        assert anonymous.status_code == 401
        login(client)
        foreign = save_settings(client, {"SERVER_NAME": "Asgard"}, origin="http://evil.example")
        assert foreign.status_code == 403

    assert env_file.read_text(encoding="utf-8") == before
    assert docker.containers.registry, "no container may be removed by a refused save"


def test_keys_outside_the_editable_set_are_ignored(stack, env_file):
    docker = stack["docker"]
    docker.seed_stopped()

    with TestClient(stack["app"]) as client:
        login(client)
        response = save_settings(
            client, {"SERVER_NAME": "Asgard", "TZ": "Europe/Oslo", "SERVER_ARGS": "-crossplay"}
        )

    assert response.status_code == 200, response.text
    assert response.json()["changed"] == ["SERVER_NAME"]
    values = parse_env_text(env_file.read_text(encoding="utf-8"))
    assert values["TZ"] == "Etc/UTC"
    assert "SERVER_ARGS" not in values


def test_the_panel_offers_editing_and_says_what_a_save_does(stack):
    with TestClient(stack["app"]) as client:
        login(client)
        page = client.get("/").text

    assert 'id="btn-settings-edit"' in page
    assert 'id="settings-form"' in page
    # The two promises the spec makes in the UI itself. Collapsed whitespace, because
    # the template wraps these sentences across lines.
    words = " ".join(page.split())
    assert "and the new settings take effect" in words
    assert "Your world and your backups are never touched" in words
    # And the claim this feature removes.
    assert "Not editable here yet" not in page


@pytest.mark.parametrize(
    "phase,action_message",
    [
        (PHASE_PULLING, "Pulling ghcr.io/example/valheim:latest (this can take a while)..."),
        (PHASE_CREATING, "Creating the Valheim container..."),
        (PHASE_STARTING, "Starting the container..."),
        (PHASE_STOPPING, "Stopping gracefully (SIGTERM, 7s grace)..."),
    ],
)
def test_a_save_during_an_in_flight_action_is_refused_and_says_to_wait(
    stack, env_file, phase, action_message
):
    """The one refusal reason that is manager state rather than container state. Its
    advice differs on purpose: mid-pull there is nothing running to stop yet, and
    mid-stop the operator has already asked for it."""
    control = stack["control"]
    # No container status produces these phases -- the real pull/create/start/stop paths
    # raise them exactly this way, from inside the engine call.
    control._set_action(phase, action_message)
    before = env_file.read_text(encoding="utf-8")

    with TestClient(stack["app"]) as client:
        login(client)
        response = save_settings(client, {"SERVER_NAME": "Asgard"})

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert action_message in error
    assert "Give it a moment" in error
    # Telling the operator to stop a server that is mid-pull, or mid-stop already, is
    # the wrong advice.
    assert "Stop the server before" not in error
    assert env_file.read_text(encoding="utf-8") == before


def test_every_refusal_carries_a_fresh_view_of_the_panel(stack):
    """One payload shape for every refusal. A save that is both invalid *and* racing a
    start would otherwise leave the editor open over a panel that is now locked."""
    docker = stack["docker"]
    docker.seed_stopped()

    with TestClient(stack["app"]) as client:
        login(client)
        invalid = save_settings(client, {"SERVER_PORT": "70000"})
        docker.containers.registry[CONTAINER].status = "running"
        racing = save_settings(client, {"SERVER_PORT": "70000"})

    for response, expected_phase in ((invalid, "stopped"), (racing, "running")):
        payload = response.json()
        assert payload["saved"] is False, response.text
        assert payload["status"]["phase"] == expected_phase
        assert {row["key"] for row in payload["settings"]} >= {"SERVER_NAME", "SERVER_PASS"}
        assert payload["settings_error"] is None


@pytest.mark.parametrize("value", [None, 2470.0, {"port": 2470}, ["2470"]])
def test_a_known_key_sent_as_an_unusable_type_is_refused_not_ignored(stack, env_file, value):
    """Ignoring an *unknown* key is deliberate. Ignoring a known one because its type is
    unusable would answer "nothing changed" to an operator who changed something."""
    docker = stack["docker"]
    container = docker.seed_stopped()
    before = env_file.read_text(encoding="utf-8")

    with TestClient(stack["app"]) as client:
        login(client)
        response = save_settings(client, {"SERVER_PORT": value})

    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert "SERVER_PORT" in error
    assert "no value changed" not in error
    assert env_file.read_text(encoding="utf-8") == before
    assert container.removed is False


def test_a_container_that_vanished_before_the_removal_is_not_called_a_failure(stack, env_file):
    """Removed on the host, or by a racing save, between the inspect and the remove. The
    end state the save wanted is the one it got, so sending the operator to `docker rm`
    for a container that no longer exists would be a lie."""
    docker = stack["docker"]
    container = docker.seed_stopped()

    def vanished(force: bool = False):
        raise NotFound("no such container: valheim-server")

    container.remove = vanished

    with TestClient(stack["app"]) as client:
        login(client)
        response = save_settings(client, {"SERVER_NAME": "Asgard"})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["saved"] is True
    assert payload["container_removed"] is False
    assert "docker rm" not in payload["message"]
    assert "SERVER_NAME=Asgard" in env_file.read_text(encoding="utf-8")


def test_the_panel_takes_the_mask_and_the_password_minimum_from_their_definitions(stack):
    """The markup used to carry its own copies of both, next to the Python that defines
    them, with nothing keeping the two honest."""
    with TestClient(stack["app"]) as client:
        login(client)
        page = client.get("/").text

    assert f'data-mask="{MASK}"' in page
    assert f"<code>{MASK}</code>" in page
    assert f'minlength="{MIN_SERVER_PASS_LENGTH}"' in page
    assert f"at least {MIN_SERVER_PASS_LENGTH}" in " ".join(page.split())


# =====================================================================
# Valheim's world modifiers in the settings panel: SERVER_ARGS.
#
# Three properties carry the feature, and each one is a rule about the *string*:
# the order it is composed in (a preset after a modifier flattens it back), the
# defaults it leaves out (`normal` is not a documented argument value), and the
# arguments it did not write but must never lose.
# =====================================================================


def save_modifiers(client: TestClient, modifiers: dict, *, settings: dict | None = None):
    return client.post(
        "/api/settings",
        json={"settings": settings or {}, "modifiers": modifiers},
        headers={"Origin": ORIGIN},
    )


def stored_args(env_file) -> str:
    return parse_env_text(env_file.read_text(encoding="utf-8")).get("SERVER_ARGS", "")


# ------------------------------------------------------------ the composer


def test_the_preset_is_composed_first_whatever_order_the_choices_arrive_in():
    """Ordering is correctness, not style: Valheim applies these in sequence, so a
    `-preset` emitted after a `-modifier` silently flattens it back to the preset's
    value. Pinned here rather than trusted to a dict's insertion order or a form's
    field order -- `raids` is inserted before `combat` on purpose."""
    composed = compose(
        Modifiers(
            preset="casual",
            categories={"raids": "more", "combat": "hard"},
            toggles=("nomap", "passivemobs"),
        )
    )

    assert composed == (
        "-preset casual -modifier combat hard -modifier raids more "
        "-setkey passivemobs -setkey nomap"
    )
    assert composed.index("-preset") < composed.index("-modifier")
    assert composed.index("-modifier") < composed.index("-setkey")


def test_a_category_at_its_default_is_left_out_not_written_as_normal():
    """`normal` is not a documented argument value for any category, so writing
    `-modifier combat normal` risks the server rejecting a flag it never advertised.
    Leaving the argument out is the documented way to keep the default."""
    assert compose(Modifiers(categories={"combat": "normal", "raids": "more"})) == (
        "-modifier raids more"
    )
    assert compose(Modifiers(preset="normal")) == ""
    assert compose(Modifiers()) == ""
    # And by the same rule it is read as the default rather than as an override.
    assert parse("-preset normal -modifier combat normal") == Modifiers()


def test_parse_and_compose_round_trip_an_existing_value():
    value = "-preset hard -modifier raids more -setkey nomap"
    parsed = parse(value)

    assert parsed.preset == "hard"
    assert dict(parsed.categories) == {"raids": "more"}
    assert parsed.toggles == ("nomap",)
    assert parsed.unmanaged == ()
    assert compose(parsed) == value


def test_every_documented_value_is_accepted_and_nothing_else_is():
    """The "only Valheim's documented vocabulary" rule, both halves of it."""
    for preset in PRESETS:
        fields_to_modifiers({"preset": preset})
    for category, values in CATEGORIES.items():
        for value in values:
            fields_to_modifiers({category: value})
    fields_to_modifiers({"toggles": list(TOGGLES)})

    for bad in (
        {"preset": "nightmare"},
        {"combat": "brutal"},
        {"raids": "hourly"},
        {"portals": "none"},  # a real value, but for `raids`, not for `portals`
        {"toggles": ["godmode"]},
        {"difficulty": "hard"},  # a category Valheim does not have
    ):
        with pytest.raises(ModifierError):
            fields_to_modifiers(bad)


def test_an_operators_own_arguments_are_kept_verbatim_after_the_modifiers():
    parsed = parse("-crossplay -modifier combat hard -saveinterval 900")

    assert dict(parsed.categories) == {"combat": "hard"}
    assert parsed.unmanaged == ("-crossplay", "-saveinterval", "900")
    # After the managed arguments, in the order they were written, duplicates and all.
    assert compose(parsed) == "-modifier combat hard -crossplay -saveinterval 900"
    assert compose(parse("-x -x")) == "-x -x"


def test_a_quoted_unmanaged_argument_keeps_its_quoting():
    """Splitting on whitespace alone would tear `-name "My Server"` in half."""
    parsed = parse('-setkey nomap -name "My Server"')

    assert parsed.toggles == ("nomap",)
    assert compose(parsed) == '-setkey nomap -name "My Server"'


def test_an_unbalanced_quote_still_opens_the_panel():
    """A typo in an argument this feature does not even manage must not make the
    settings file unreadable to the editor."""
    assert compose(parse('-name "unterminated')) == '-name "unterminated'


def test_a_preset_written_after_a_modifier_is_read_as_flattening_it():
    """`-modifier combat hard -preset casual` is entirely casual on a real server, so
    showing the operator a combat override the game is about to discard would be a
    lie -- and re-composing it preset-first would change what the server does."""
    parsed = parse("-modifier combat hard -preset casual -modifier raids more")

    assert parsed.preset == "casual"
    assert dict(parsed.categories) == {"raids": "more"}
    assert compose(parsed) == "-preset casual -modifier raids more"


def test_an_unknown_token_is_not_silently_dropped():
    """Undocumented values are left alone on the way in -- a file the operator
    hand-edited still has to open in the panel -- and are refused on the way in from a
    form, where naming the field is possible."""
    parsed = parse("-modifier combat brutal")

    assert dict(parsed.categories) == {}
    assert compose(parsed) == "-modifier combat brutal"


def test_a_ready_made_server_args_string_is_refused_by_the_shared_validator():
    """Modifiers arrive as fields and are composed by the manager. A string that is not
    what the composer would have produced cannot reach the file, which is what lets the
    panel promise its preview is the value that gets written."""
    assert validated_settings({"SERVER_ARGS": "-preset hard"}) == {
        "SERVER_ARGS": "-preset hard"
    }
    assert validated_settings({"SERVER_ARGS": ""}) == {"SERVER_ARGS": ""}

    with pytest.raises(SetupInputError) as excinfo:
        validated_settings({"SERVER_ARGS": "-modifier combat hard -preset casual"})
    assert "-preset first" in str(excinfo.value)


def test_composing_from_fields_carries_the_unmanaged_arguments_through():
    assert (
        validated_modifiers({"raids": "more"}, current="-crossplay -modifier combat hard")
        == "-modifier raids more -crossplay"
    )

    with pytest.raises(SetupInputError) as excinfo:
        validated_modifiers({"combat": "brutal"})
    assert "combat" in str(excinfo.value)


# --------------------------------------------------------- through the panel


def test_a_preset_two_overrides_and_two_toggles_are_saved_in_the_required_order(
    stack, env_file
):
    docker = stack["docker"]
    container = docker.seed_stopped()

    with TestClient(stack["app"]) as client:
        login(client)
        response = save_modifiers(
            client,
            {
                "preset": "hard",
                "combat": "veryhard",
                "raids": "more",
                "deathpenalty": "",
                "resources": "",
                "portals": "",
                "toggles": ["nomap", "passivemobs"],
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["saved"] is True
    assert payload["changed"] == ["SERVER_ARGS"]
    assert payload["container_removed"] is True
    assert container.removed is True
    # Preset -> modifiers -> setkeys, with the three defaulted categories absent.
    assert stored_args(env_file) == (
        "-preset hard -modifier combat veryhard -modifier raids more "
        "-setkey passivemobs -setkey nomap"
    )
    # The panel's own refresh already shows what the controls should say.
    assert payload["modifiers"]["preset"] == "hard"
    assert payload["modifiers"]["categories"] == {"combat": "veryhard", "raids": "more"}
    assert payload["modifiers"]["toggles"] == ["passivemobs", "nomap"]
    assert payload["modifiers"]["server_args"] == stored_args(env_file)


def test_the_next_start_hands_the_composed_arguments_to_the_new_container(stack, env_file):
    docker = stack["docker"]
    docker.seed_stopped()

    with TestClient(stack["app"]) as client:
        login(client)
        assert save_modifiers(client, {"preset": "hardcore"}).status_code == 200
        assert client.post("/api/start", headers={"Origin": ORIGIN}).status_code == 200

    assert docker.containers.create_calls[-1]["environment"]["SERVER_ARGS"] == (
        "-preset hardcore"
    )


def test_the_editor_opens_on_exactly_the_modifiers_the_file_encodes(stack, env_file):
    env_file.write_text(
        ENV_FILE_TEXT + "SERVER_ARGS=-preset hard -modifier raids more -setkey nomap\n",
        encoding="utf-8",
    )

    with TestClient(stack["app"]) as client:
        login(client)
        payload = client.get("/api/status").json()

    assert payload["modifiers"] == {
        "preset": "hard",
        "categories": {"raids": "more"},
        "toggles": ["nomap"],
        "unmanaged": "",
        "server_args": "-preset hard -modifier raids more -setkey nomap",
    }


def test_no_modifiers_yet_reads_as_every_default_and_an_empty_preview(stack, env_file):
    """`SERVER_ARGS` absent from the file entirely, which is what an install that
    predates this feature looks like."""
    assert "SERVER_ARGS" not in env_file.read_text(encoding="utf-8")

    with TestClient(stack["app"]) as client:
        login(client)
        payload = client.get("/api/status").json()

    assert payload["modifiers"] == {
        "preset": "",
        "categories": {},
        "toggles": [],
        "unmanaged": "",
        "server_args": "",
    }


def test_changing_a_category_keeps_an_argument_the_panel_does_not_manage(stack, env_file):
    """An operator who added their own flag must not lose it by touching a dropdown."""
    env_file.write_text(
        ENV_FILE_TEXT + 'SERVER_ARGS="-crossplay -modifier combat hard"\n',
        encoding="utf-8",
    )
    docker = stack["docker"]
    docker.seed_stopped()

    with TestClient(stack["app"]) as client:
        login(client)
        # What the prefilled controls would post: combat as the file has it, plus the
        # raid frequency the operator just changed.
        response = save_modifiers(client, {"combat": "hard", "raids": "more"})

    assert response.status_code == 200, response.text
    assert stored_args(env_file) == "-modifier combat hard -modifier raids more -crossplay"
    assert response.json()["modifiers"]["unmanaged"] == "-crossplay"


def test_resetting_one_category_removes_only_its_own_argument(stack, env_file):
    env_file.write_text(
        ENV_FILE_TEXT + 'SERVER_ARGS="-modifier combat hard -modifier raids more"\n',
        encoding="utf-8",
    )
    docker = stack["docker"]
    docker.seed_stopped()

    with TestClient(stack["app"]) as client:
        login(client)
        response = save_modifiers(client, {"combat": "", "raids": "more"})

    assert response.status_code == 200, response.text
    assert stored_args(env_file) == "-modifier raids more"


@pytest.mark.parametrize(
    "modifiers,expected",
    [
        ({"preset": "nightmare"}, "preset"),
        ({"combat": "brutal"}, "combat"),
        ({"deathpenalty": "none"}, "deathpenalty"),
        ({"toggles": ["godmode"]}, "godmode"),
        ({"difficulty": "hard"}, "difficulty"),
        ({"combat": 3}, "combat"),
        ({"raids": True}, "raids"),
        ({"toggles": "nomap"}, "toggles"),
    ],
)
def test_an_undocumented_modifier_is_refused_naming_the_field(
    stack, env_file, modifiers, expected
):
    docker = stack["docker"]
    container = docker.seed_stopped()
    before = env_file.read_text(encoding="utf-8")

    with TestClient(stack["app"]) as client:
        login(client)
        response = save_modifiers(client, modifiers)

    assert response.status_code == 400, response.text
    assert expected in response.json()["error"]
    assert response.json()["saved"] is False
    # Nothing written, and no container removed: the same promise every other refusal
    # on this route makes.
    assert env_file.read_text(encoding="utf-8") == before
    assert container.removed is False


def test_modifiers_sent_as_something_other_than_an_object_are_refused(stack, env_file):
    before = env_file.read_text(encoding="utf-8")

    with TestClient(stack["app"]) as client:
        login(client)
        response = client.post(
            "/api/settings",
            json={"modifiers": "-preset hard"},
            headers={"Origin": ORIGIN},
        )

    assert response.status_code == 400, response.text
    assert "modifiers" in response.json()["error"]
    assert env_file.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("state", ["running", "restarting", "paused"])
def test_modifiers_cannot_be_saved_while_the_server_is_live(stack, env_file, state):
    docker = stack["docker"]
    container = docker.seed_stopped()
    container.status = state
    before = env_file.read_text(encoding="utf-8")

    with TestClient(stack["app"]) as client:
        login(client)
        response = save_modifiers(client, {"preset": "hard"})

    assert response.status_code == 409, response.text
    assert response.json()["saved"] is False
    assert env_file.read_text(encoding="utf-8") == before
    assert container.removed is False


def test_a_save_that_changes_no_modifier_writes_nothing(stack, env_file):
    env_file.write_text(ENV_FILE_TEXT + "SERVER_ARGS=-preset hard\n", encoding="utf-8")
    docker = stack["docker"]
    container = docker.seed_stopped()
    before = env_file.read_text(encoding="utf-8")

    with TestClient(stack["app"]) as client:
        login(client)
        response = save_modifiers(client, {"preset": "hard"})

    assert response.status_code == 200, response.text
    assert response.json()["changed"] == []
    assert container.removed is False
    assert env_file.read_text(encoding="utf-8") == before


def test_a_save_without_a_modifiers_object_leaves_server_args_alone(stack, env_file):
    """`None` and `{}` are different answers: a request that does not carry the
    modifiers at all must not read as "every category back to default"."""
    env_file.write_text(ENV_FILE_TEXT + "SERVER_ARGS=-preset hard\n", encoding="utf-8")
    docker = stack["docker"]
    docker.seed_stopped()

    with TestClient(stack["app"]) as client:
        login(client)
        response = save_settings(client, {"SERVER_NAME": "Asgard"})

    assert response.status_code == 200, response.text
    assert response.json()["changed"] == ["SERVER_NAME"]
    assert stored_args(env_file) == "-preset hard"


def test_an_empty_modifiers_object_clears_the_managed_arguments(stack, env_file):
    env_file.write_text(
        ENV_FILE_TEXT + 'SERVER_ARGS="-preset hard -setkey nomap -crossplay"\n',
        encoding="utf-8",
    )
    docker = stack["docker"]
    docker.seed_stopped()

    with TestClient(stack["app"]) as client:
        login(client)
        response = save_modifiers(client, {})

    assert response.status_code == 200, response.text
    # The operator's own flag is not a modifier, so it is not cleared with them.
    assert stored_args(env_file) == "-crossplay"


def test_the_composed_value_survives_the_env_file_round_trip(stack, env_file):
    """The value has spaces in it, so the writer has to quote it and the reader has to
    read it back unchanged -- otherwise every save would look like a change."""
    docker = stack["docker"]
    docker.seed_stopped()

    with TestClient(stack["app"]) as client:
        login(client)
        first = save_modifiers(client, {"preset": "hard", "toggles": ["nomap"]})
        second = save_modifiers(client, {"preset": "hard", "toggles": ["nomap"]})

    assert first.json()["changed"] == ["SERVER_ARGS"]
    assert second.json()["changed"] == []
    text = env_file.read_text(encoding="utf-8")
    assert 'SERVER_ARGS="-preset hard -setkey nomap"' in text


# ------------------------------------------------------------- the surfaces


def test_the_panel_renders_the_vocabulary_in_the_composers_own_order(stack):
    """The preview walks these controls in document order, so the markup's order *is*
    the composed order. Rendered from app/modifiers.py rather than hand-written, and
    checked here so a future category cannot land in the wrong place."""
    with TestClient(stack["app"]) as client:
        login(client)
        page = client.get("/").text

    positions = [page.index('data-modifier-field="preset"')]
    for category in CATEGORIES:
        assert f'data-modifier-field="{category}"' in page
        positions.append(page.index(f'data-modifier-field="{category}"'))
    assert positions == sorted(positions)

    toggle_positions = []
    for toggle in TOGGLES:
        assert f'data-modifier-toggle="{toggle}"' in page
        toggle_positions.append(page.index(f'data-modifier-toggle="{toggle}"'))
    assert toggle_positions == sorted(toggle_positions)
    assert max(positions) < min(toggle_positions)

    for value in ("hardcore", "veryhard", "muchmore", "casual"):
        assert f'<option value="{value}">' in page
    # `normal` is never an option to pick: the default is the absence of the argument.
    assert 'value="normal"' not in page
    assert 'id="modifier-preview"' in page


def test_the_panel_says_modifiers_are_rules_and_not_the_world(stack):
    with TestClient(stack["app"]) as client:
        login(client)
        words = " ".join(client.get("/").text.split())

    # The preview is still there -- what changed is that the panel no longer explains
    # the argument string to get the point across.
    assert 'id="modifier-preview"' in words
    assert "never your existing world" in words


def test_the_first_run_wizard_offers_no_modifiers(fresh):
    """Post-install only. The wizard writes SERVER_ARGS nowhere, so a fresh install has
    the documented empty line and nothing else."""
    with TestClient(fresh["app"]) as client:
        page = client.get("/setup", params={"token": fresh["token"]}).text
        assert "data-modifier-field" not in page
        assert "SERVER_ARGS" not in page
        assert complete_setup(client, fresh["token"]).status_code == 303

    values = parse_env_text(fresh["settings"].path.read_text(encoding="utf-8"))
    assert values["SERVER_ARGS"] == ""


def test_the_shipped_example_and_the_written_default_both_document_server_args(
    project_root,
):
    """Both files carry the key so the panel writes it in place, under the comment that
    explains it, rather than appending a bare line at the bottom."""
    example = (project_root / "valheim.env.example").read_text(encoding="utf-8")
    assert parse_env_text(example)["SERVER_ARGS"] == ""
    assert parse_env_text(DEFAULT_SETTINGS_TEXT)["SERVER_ARGS"] == ""
    for text in (example, DEFAULT_SETTINGS_TEXT):
        assert "-preset" in text and "-modifier" in text and "-setkey" in text


def test_saving_a_modifier_writes_it_under_the_documented_comment(tmp_path, fake_docker):
    """The key exists in the shipped file, so `write` replaces it in place: no
    "Added by the Valheim manager" block, and the comment stays above it."""
    path = tmp_path / "valheim.env"
    path.write_text(DEFAULT_SETTINGS_TEXT, encoding="utf-8")
    config = build_config(path)
    app = create_app(
        config=config,
        controller=build_control(config, fake_docker),
        settings=SettingsStore(path),
    )
    fake_docker.seed_stopped()

    with TestClient(app) as client:
        login(client)
        assert save_modifiers(client, {"preset": "hard"}).status_code == 200

    text = path.read_text(encoding="utf-8")
    # Quoted, because the value has a space in it -- and read back unquoted.
    assert 'SERVER_ARGS="-preset hard"' in text
    assert parse_env_text(text)["SERVER_ARGS"] == "-preset hard"
    assert "Added by the Valheim manager" not in text
    # The comment block that documents the key is still above it.
    assert text.index("Extra command-line arguments") < text.index("SERVER_ARGS=")


# ============================================================== the Worlds panel
#
# The spec's world matrix, end to end: listing and classification, switching through
# the settings path, an upload of each accepted shape, and every refusal -- a lone
# `.db`, a traversal entry, a name collision, an oversize drop, and any of it while
# the server is running. What every refusal has to prove is the same thing: that the
# volume was left exactly as it was found.


WORLDS_CAP_BYTES = 2 * 1024 * 1024


@pytest.fixture
def worlds_dir(tmp_path):
    path = tmp_path / "worlds_local"
    path.mkdir()
    return path


@pytest.fixture
def worlds(env_file, fake_docker, worlds_dir):
    config = build_config(
        env_file, worlds_dir=str(worlds_dir), world_upload_max_bytes=WORLDS_CAP_BYTES
    )
    control = build_control(config, fake_docker)
    app = create_app(config=config, controller=control, settings=SettingsStore(env_file))
    return {
        "app": app,
        "config": config,
        "control": control,
        "docker": fake_docker,
        "dir": worlds_dir,
        "env": env_file,
    }


def make_modern_world(root, name: str, *, chunks: int = 2):
    """A 1.0 world in the layout a live server actually leaves on the volume."""
    world = root / name
    world.mkdir()
    (world / "_main.1.db2").write_bytes(b"world-data" * 8)
    (world / "_main.1.fwl2").write_bytes(b"meta")
    (world / "_main.1.chunks").write_bytes(b"index")
    (world / "_main.1.ok").write_bytes(b"")
    for index in range(chunks):
        (world / f"{index}_0.chunk").write_bytes(b"chunk")
    return world


def make_legacy_world(root, name: str) -> None:
    (root / f"{name}.db").write_bytes(b"legacy-data")
    (root / f"{name}.fwl").write_bytes(b"legacy-meta")


def zip_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return buffer.getvalue()


def upload_world(client: TestClient, parts, *, name: str = "", origin: str = ORIGIN):
    """POST a multipart upload. ``parts`` is a list of (filename, bytes) pairs, and the
    filename is the path within the drop -- exactly what the browser sends."""
    files = [
        ("files", (filename, payload, "application/octet-stream"))
        for filename, payload in parts
    ]
    return client.post(
        "/api/worlds/upload", files=files, data={"name": name}, headers={"Origin": origin}
    )


def switch_world(client: TestClient, name: str, *, origin: str = ORIGIN):
    return client.post(
        "/api/worlds/switch", json={"name": name}, headers={"Origin": origin}
    )


def volume_snapshot(root) -> dict:
    """Every file on the volume with its contents: what a refusal must not change."""
    snapshot = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(root)).replace("\\", "/")] = path.read_bytes()
    return snapshot


# ------------------------------------------------------------------- listing


def test_worlds_are_listed_with_their_layout_size_and_the_active_one_marked(worlds):
    make_modern_world(worlds["dir"], "Dedicated")
    make_legacy_world(worlds["dir"], "Grandfathered")

    with TestClient(worlds["app"]) as client:
        login(client)
        payload = client.get("/api/worlds").json()

    rows = {row["name"]: row for row in payload["worlds"]}
    assert set(rows) == {"Dedicated", "Grandfathered"}
    assert rows["Dedicated"]["layout"] == "1.0"
    assert rows["Dedicated"]["legacy"] is False
    assert rows["Grandfathered"]["layout"] == "legacy"
    assert rows["Grandfathered"]["legacy"] is True
    assert rows["Dedicated"]["size_bytes"] > 0
    assert rows["Dedicated"]["size"]
    # WORLD_NAME in the settings file is `Dedicated`, so that is the active one.
    assert payload["active_world"] == "Dedicated"
    assert rows["Dedicated"]["active"] is True
    assert rows["Grandfathered"]["active"] is False
    assert payload["worlds_error"] is None
    assert payload["max_upload_bytes"] == WORLDS_CAP_BYTES


def test_a_directory_that_is_not_a_world_is_not_listed_as_one(worlds):
    (worlds["dir"] / "not-a-world").mkdir()
    (worlds["dir"] / "not-a-world" / "readme.txt").write_text("hello", encoding="utf-8")
    # A half-world: the `.db2` with no `.fwl2` beside it.
    (worlds["dir"] / "halfway").mkdir()
    (worlds["dir"] / "halfway" / "_main.1.db2").write_bytes(b"x")
    # And a lone legacy file, which is not a world either.
    (worlds["dir"] / "Lonely.db").write_bytes(b"x")

    with TestClient(worlds["app"]) as client:
        login(client)
        payload = client.get("/api/worlds").json()

    assert payload["worlds"] == []
    assert payload["worlds_error"] is None


def test_an_empty_volume_says_so_and_the_panel_still_renders(worlds):
    with TestClient(worlds["app"]) as client:
        login(client)
        payload = client.get("/api/worlds").json()
        page = client.get("/").text

    assert payload["worlds"] == []
    assert payload["worlds_error"] is None
    # The panel's own wording: nothing there yet, and the first Start makes one.
    assert "No worlds yet" in page
    assert "Valheim makes one for you" in page


def test_a_worlds_directory_that_cannot_be_read_is_named_not_crashed(
    env_file, fake_docker, tmp_path
):
    """A file where the directory should be: the panel still renders, with the reason."""
    blocked = tmp_path / "worlds_local_file"
    blocked.write_text("not a directory", encoding="utf-8")
    config = build_config(env_file, worlds_dir=str(blocked))
    app = create_app(
        config=config,
        controller=build_control(config, fake_docker),
        settings=SettingsStore(env_file),
    )
    with TestClient(app) as client:
        login(client)
        payload = client.get("/api/worlds").json()
        assert client.get("/").status_code == 200

    assert payload["worlds"] == []
    assert str(blocked) in payload["worlds_error"]


def test_listing_needs_a_session(worlds):
    with TestClient(worlds["app"]) as client:
        assert client.get("/api/worlds").status_code == 401


# ------------------------------------------------------------------ switching


def test_switching_writes_world_name_and_removes_the_stopped_container(worlds):
    make_modern_world(worlds["dir"], "Dedicated")
    make_modern_world(worlds["dir"], "Seedy")
    container = worlds["docker"].seed_stopped()

    with TestClient(worlds["app"]) as client:
        login(client)
        response = switch_world(client, "Seedy")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["saved"] is True
    assert payload["changed"] == ["WORLD_NAME"]
    assert payload["container_removed"] is True
    assert container.removed is True
    assert "WORLD_NAME=Seedy" in worlds["env"].read_text(encoding="utf-8")
    # The answer carries the refreshed panel, so the list marks the new active world
    # and the settings table shows the new value without waiting for a status push.
    assert payload["active_world"] == "Seedy"
    assert [row["name"] for row in payload["worlds"] if row["active"]] == ["Seedy"]
    rows = {row["key"]: row["value"] for row in payload["settings"]}
    assert rows["WORLD_NAME"] == "Seedy"


def test_the_next_start_loads_the_switched_world(worlds):
    make_modern_world(worlds["dir"], "Seedy")
    worlds["docker"].seed_stopped()

    with TestClient(worlds["app"]) as client:
        login(client)
        assert switch_world(client, "Seedy").status_code == 200
        assert client.post("/api/start", headers={"Origin": ORIGIN}).status_code == 200

    created = worlds["docker"].containers.create_calls[-1]
    assert created["environment"]["WORLD_NAME"] == "Seedy"


@pytest.mark.parametrize("state", ["running", "restarting", "paused"])
def test_switching_is_refused_while_the_server_is_live(worlds, state):
    make_modern_world(worlds["dir"], "Seedy")
    container = worlds["docker"].seed_stopped()
    container.status = state
    before = worlds["env"].read_text(encoding="utf-8")

    with TestClient(worlds["app"]) as client:
        login(client)
        response = switch_world(client, "Seedy")

    assert response.status_code == 409
    assert "Turn the server off first" in response.json()["error"]
    assert worlds["env"].read_text(encoding="utf-8") == before
    assert container.removed is False


def test_switching_to_a_world_that_is_not_there_is_refused(worlds):
    make_modern_world(worlds["dir"], "Dedicated")
    before = worlds["env"].read_text(encoding="utf-8")

    with TestClient(worlds["app"]) as client:
        login(client)
        response = switch_world(client, "Imaginary")

    assert response.status_code == 404
    assert "Imaginary" in response.json()["error"]
    assert worlds["env"].read_text(encoding="utf-8") == before


@pytest.mark.parametrize("name", ["../escape", "a/b", "", ".", "   ", "a\\b", "x\x00y"])
def test_a_world_name_that_is_not_one_segment_is_refused(worlds, name):
    before = worlds["env"].read_text(encoding="utf-8")

    with TestClient(worlds["app"]) as client:
        login(client)
        response = switch_world(client, name)

    assert response.status_code == 400, response.text
    assert worlds["env"].read_text(encoding="utf-8") == before


def test_switching_to_the_active_world_writes_nothing_and_keeps_the_container(worlds):
    make_modern_world(worlds["dir"], "Dedicated")
    container = worlds["docker"].seed_stopped()
    before = worlds["env"].read_text(encoding="utf-8")

    with TestClient(worlds["app"]) as client:
        login(client)
        response = switch_world(client, "Dedicated")

    assert response.status_code == 200
    assert response.json()["changed"] == []
    assert response.json()["container_removed"] is False
    assert container.removed is False
    assert worlds["env"].read_text(encoding="utf-8") == before


def test_switching_needs_a_session_and_a_same_origin_post(worlds):
    make_modern_world(worlds["dir"], "Seedy")
    before = worlds["env"].read_text(encoding="utf-8")

    with TestClient(worlds["app"]) as client:
        assert (
            client.post(
                "/api/worlds/switch", json={"name": "Seedy"}, headers={"Origin": ORIGIN}
            ).status_code
            == 401
        )
        login(client)
        assert switch_world(client, "Seedy", origin="http://evil.example").status_code == 403
        assert client.post("/api/worlds/switch", json={"name": "Seedy"}).status_code == 403

    assert worlds["env"].read_text(encoding="utf-8") == before


# ------------------------------------------------------------------ uploading


def test_a_dropped_1_0_world_folder_is_accepted_and_listed(worlds):
    parts = [
        ("Imported/_main.1.db2", b"world-data" * 64),
        ("Imported/_main.1.fwl2", b"meta"),
        ("Imported/_main.1.chunks", b"index"),
        ("Imported/_main.1.ok", b""),
        ("Imported/0_0.chunk", b"chunk"),
    ]
    with TestClient(worlds["app"]) as client:
        login(client)
        response = upload_world(client, parts)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["uploaded"] is True
    assert payload["name"] == "Imported"
    assert payload["layout"] == "1.0"
    assert payload["warning"] is None
    assert [row["name"] for row in payload["worlds"]] == ["Imported"]

    placed = worlds["dir"] / "Imported"
    assert sorted(path.name for path in placed.iterdir()) == [
        "0_0.chunk",
        "_main.1.chunks",
        "_main.1.db2",
        "_main.1.fwl2",
        "_main.1.ok",
    ]
    assert (placed / "_main.1.db2").read_bytes() == b"world-data" * 64
    # And no staging directory left behind.
    assert [path.name for path in worlds["dir"].iterdir()] == ["Imported"]


def test_a_zip_of_a_world_folder_is_accepted(worlds):
    archive = zip_bytes(
        [
            ("Imported/_main.1.db2", b"data"),
            ("Imported/_main.1.fwl2", b"meta"),
            ("Imported/_main.1.ok", b""),
        ]
    )
    with TestClient(worlds["app"]) as client:
        login(client)
        response = upload_world(client, [("Imported.zip", archive)])

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Imported"
    assert (worlds["dir"] / "Imported" / "_main.1.db2").read_bytes() == b"data"


def test_an_uploaded_world_can_be_switched_to_and_the_next_start_loads_it(worlds):
    with TestClient(worlds["app"]) as client:
        login(client)
        assert (
            upload_world(
                client,
                [("Imported/_main.1.db2", b"data"), ("Imported/_main.1.fwl2", b"meta")],
            ).status_code
            == 200
        )
        assert switch_world(client, "Imported").status_code == 200
        assert client.post("/api/start", headers={"Origin": ORIGIN}).status_code == 200

    created = worlds["docker"].containers.create_calls[-1]
    assert created["environment"]["WORLD_NAME"] == "Imported"


def test_a_legacy_pair_is_accepted_with_the_conversion_warning(worlds):
    with TestClient(worlds["app"]) as client:
        login(client)
        response = upload_world(
            client, [("Grandfathered.db", b"data"), ("Grandfathered.fwl", b"meta")]
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["layout"] == "legacy"
    assert payload["name"] == "Grandfathered"
    # Not a footnote: loading it rewrites it, and there is no way back.
    assert "permanently" in payload["warning"]
    assert (worlds["dir"] / "Grandfathered.db").read_bytes() == b"data"
    assert (worlds["dir"] / "Grandfathered.fwl").read_bytes() == b"meta"
    assert payload["worlds"][0]["layout"] == "legacy"


def test_an_upload_can_be_given_a_different_name(worlds):
    with TestClient(worlds["app"]) as client:
        login(client)
        response = upload_world(
            client,
            [("Imported/_main.1.db2", b"data"), ("Imported/_main.1.fwl2", b"meta")],
            name="Renamed",
        )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Renamed"
    assert (worlds["dir"] / "Renamed" / "_main.1.db2").is_file()
    assert not (worlds["dir"] / "Imported").exists()


@pytest.mark.parametrize(
    "parts, expected",
    [
        ([("Only/_main.1.db2", b"data")], "_main.N.fwl2"),
        ([("Only/_main.1.fwl2", b"meta")], "_main.N.db2"),
        ([("Only/readme.txt", b"hello")], "_main.N.db2"),
    ],
)
def test_an_upload_that_is_not_a_world_is_refused_naming_what_is_missing(
    worlds, parts, expected
):
    before = volume_snapshot(worlds["dir"])
    with TestClient(worlds["app"]) as client:
        login(client)
        response = upload_world(client, parts)

    assert response.status_code == 400, response.text
    assert expected in response.json()["error"]
    assert volume_snapshot(worlds["dir"]) == before


@pytest.mark.parametrize("lone", ["Grandfathered.db", "Grandfathered.fwl"])
def test_a_lone_legacy_file_says_both_are_required(worlds, lone):
    before = volume_snapshot(worlds["dir"])
    with TestClient(worlds["app"]) as client:
        login(client)
        response = upload_world(client, [(lone, b"data")])

    assert response.status_code == 400
    error = response.json()["error"]
    assert "Grandfathered.db and Grandfathered.fwl" in error
    missing = ".fwl" if lone.endswith(".db") else ".db"
    assert f"the {missing} one is missing" in error
    assert volume_snapshot(worlds["dir"]) == before


@pytest.mark.parametrize(
    "hostile",
    [
        "../escape.db2",
        "../../etc/passwd",
        "/etc/passwd",
        "Imported/../../escape",
        "C:/Windows/system32/evil",
        "..\\escape.txt",
    ],
)
def test_an_archive_entry_that_escapes_the_destination_is_refused(worlds, hostile):
    outside = worlds["dir"].parent
    before_outside = sorted(path.name for path in outside.iterdir())
    before = volume_snapshot(worlds["dir"])
    archive = zip_bytes(
        [
            ("Imported/_main.1.db2", b"data"),
            ("Imported/_main.1.fwl2", b"meta"),
            (hostile, b"owned"),
        ]
    )

    with TestClient(worlds["app"]) as client:
        login(client)
        response = upload_world(client, [("Imported.zip", archive)])

    assert response.status_code == 400, response.text
    error = response.json()["error"]
    # Reported as a rejected upload, not a crash, and nothing written anywhere.
    assert "Nothing was saved" in error
    assert "outside the world directory" in error
    assert volume_snapshot(worlds["dir"]) == before
    assert sorted(path.name for path in outside.iterdir()) == before_outside


def test_a_symlink_entry_in_an_archive_is_refused(worlds):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Imported/_main.1.db2", b"data")
        archive.writestr("Imported/_main.1.fwl2", b"meta")
        link = zipfile.ZipInfo("Imported/link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, b"/etc/passwd")
    before = volume_snapshot(worlds["dir"])

    with TestClient(worlds["app"]) as client:
        login(client)
        response = upload_world(client, [("Imported.zip", buffer.getvalue())])

    assert response.status_code == 400
    assert "symbolic link" in response.json()["error"]
    assert volume_snapshot(worlds["dir"]) == before


def test_an_upload_matching_an_existing_world_is_refused_and_leaves_it_alone(worlds):
    make_modern_world(worlds["dir"], "Dedicated")
    before = volume_snapshot(worlds["dir"])

    with TestClient(worlds["app"]) as client:
        login(client)
        response = upload_world(
            client,
            [("Dedicated/_main.1.db2", b"overwritten"), ("Dedicated/_main.1.fwl2", b"x")],
        )

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert "'Dedicated'" in error and "already" in error
    assert volume_snapshot(worlds["dir"]) == before


def test_a_collision_with_a_legacy_world_is_refused_too(worlds):
    make_legacy_world(worlds["dir"], "Grandfathered")
    before = volume_snapshot(worlds["dir"])

    with TestClient(worlds["app"]) as client:
        login(client)
        response = upload_world(
            client, [("Grandfathered.db", b"new"), ("Grandfathered.fwl", b"new")]
        )

    assert response.status_code == 409
    assert volume_snapshot(worlds["dir"]) == before


def test_an_oversize_upload_is_refused_with_the_cap_in_the_message(worlds):
    before = volume_snapshot(worlds["dir"])
    payload = b"x" * (WORLDS_CAP_BYTES + 1024)

    with TestClient(worlds["app"]) as client:
        login(client)
        response = upload_world(
            client, [("Big/_main.1.db2", payload), ("Big/_main.1.fwl2", b"meta")]
        )

    assert response.status_code == 413, response.text
    error = response.json()["error"]
    assert "2.0 MB" in error  # the cap, stated
    assert "WORLD_UPLOAD_MAX_MB" in error
    assert volume_snapshot(worlds["dir"]) == before


def test_an_oversize_body_is_refused_before_it_is_even_read(worlds):
    """Content-Length alone is enough to refuse, so the body never reaches any disk --
    not even the temp file the multipart parser would otherwise spool it to."""
    with TestClient(worlds["app"]) as client:
        login(client)
        response = client.post(
            "/api/worlds/upload",
            headers={
                "Origin": ORIGIN,
                "Content-Type": "multipart/form-data; boundary=x",
                "Content-Length": str(WORLDS_CAP_BYTES * 100),
            },
            content=b"",
        )

    assert response.status_code == 413
    assert "WORLD_UPLOAD_MAX_MB" in response.json()["error"]


@pytest.mark.parametrize("state", ["running", "restarting", "paused"])
def test_uploading_is_refused_while_the_server_is_live(worlds, state):
    container = worlds["docker"].seed_stopped()
    container.status = state
    before = volume_snapshot(worlds["dir"])

    with TestClient(worlds["app"]) as client:
        login(client)
        response = upload_world(
            client, [("Imported/_main.1.db2", b"data"), ("Imported/_main.1.fwl2", b"m")]
        )

    assert response.status_code == 409
    assert "Turn the server off first" in response.json()["error"]
    assert volume_snapshot(worlds["dir"]) == before


def test_a_docker_that_cannot_be_reached_refuses_a_world_action(worlds):
    make_modern_world(worlds["dir"], "Seedy")
    before = volume_snapshot(worlds["dir"])
    worlds["docker"].get_error = APIError("boom")

    with TestClient(worlds["app"]) as client:
        login(client)
        upload = upload_world(
            client, [("Imported/_main.1.db2", b"d"), ("Imported/_main.1.fwl2", b"m")]
        )
        switch = switch_world(client, "Seedy")

    assert upload.status_code == 502
    assert switch.status_code == 502
    assert "no way to check that your server is off" in upload.json()["error"]
    assert volume_snapshot(worlds["dir"]) == before


def test_uploading_needs_a_session_and_a_same_origin_post(worlds):
    parts = [("Imported/_main.1.db2", b"d"), ("Imported/_main.1.fwl2", b"m")]
    before = volume_snapshot(worlds["dir"])

    with TestClient(worlds["app"]) as client:
        assert upload_world(client, parts).status_code == 401
        login(client)
        assert upload_world(client, parts, origin="http://evil.example").status_code == 403

    assert volume_snapshot(worlds["dir"]) == before


def test_an_upload_with_no_files_is_refused(worlds):
    with TestClient(worlds["app"]) as client:
        login(client)
        response = client.post(
            "/api/worlds/upload", data={"name": "Imported"}, headers={"Origin": ORIGIN}
        )

    assert response.status_code == 400
    assert "Nothing arrived" in response.json()["error"]


def test_an_archive_holding_two_worlds_is_refused(worlds):
    archive = zip_bytes(
        [
            ("A/_main.1.db2", b"a"),
            ("A/_main.1.fwl2", b"a"),
            ("B/_main.1.db2", b"b"),
            ("B/_main.1.fwl2", b"b"),
        ]
    )
    before = volume_snapshot(worlds["dir"])

    with TestClient(worlds["app"]) as client:
        login(client)
        response = upload_world(client, [("Worlds.zip", archive)])

    assert response.status_code == 400
    assert "more than one world" in response.json()["error"]
    assert volume_snapshot(worlds["dir"]) == before


def test_a_zip_that_is_not_a_zip_is_refused(worlds):
    with TestClient(worlds["app"]) as client:
        login(client)
        response = upload_world(client, [("Imported.zip", b"not an archive at all")])

    assert response.status_code == 400
    assert "not a readable zip" in response.json()["error"]


def test_an_archive_of_the_whole_worlds_directory_still_finds_the_world(worlds):
    """`worlds_local/Imported/...` reduces to the world, and it keeps the world's own
    name rather than the wrapper's."""
    archive = zip_bytes(
        [
            ("worlds_local/Imported/_main.1.db2", b"data"),
            ("worlds_local/Imported/_main.1.fwl2", b"meta"),
        ]
    )
    with TestClient(worlds["app"]) as client:
        login(client)
        response = upload_world(client, [("backup.zip", archive)])

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Imported"


def test_macos_archive_junk_does_not_defeat_the_folder_detection(worlds):
    archive = zip_bytes(
        [
            ("Imported/_main.1.db2", b"data"),
            ("Imported/_main.1.fwl2", b"meta"),
            ("Imported/.DS_Store", b"junk"),
            ("__MACOSX/Imported/._main.1.db2", b"junk"),
        ]
    )
    with TestClient(worlds["app"]) as client:
        login(client)
        response = upload_world(client, [("Imported.zip", archive)])

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Imported"
    placed = worlds["dir"] / "Imported"
    assert sorted(path.name for path in placed.iterdir()) == [
        "_main.1.db2",
        "_main.1.fwl2",
    ]


@POSIX_ONLY
def test_an_uploaded_world_is_group_writable_so_the_game_can_save_to_it(worlds):
    """The manager cannot chown -- `cap_drop: [ALL]` takes CAP_CHOWN and it is not root
    -- so the group bit is the whole mechanism. A world written unwritable would
    surface much later as the server silently failing to save."""
    with TestClient(worlds["app"]) as client:
        login(client)
        assert (
            upload_world(
                client,
                [
                    ("Imported/_main.1.db2", b"data"),
                    ("Imported/_main.1.fwl2", b"meta"),
                    ("Imported/0_0.chunk", b"chunk"),
                ],
            ).status_code
            == 200
        )
        assert (
            upload_world(
                client, [("Grandfathered.db", b"d"), ("Grandfathered.fwl", b"m")]
            ).status_code
            == 200
        )

    placed = worlds["dir"] / "Imported"
    mode = stat.S_IMODE(placed.stat().st_mode)
    assert mode & stat.S_IWGRP, oct(mode)
    assert mode & stat.S_IXGRP, oct(mode)
    for child in placed.iterdir():
        assert stat.S_IMODE(child.stat().st_mode) & stat.S_IWGRP, child
    for name in ("Grandfathered.db", "Grandfathered.fwl"):
        child = worlds["dir"] / name
        assert stat.S_IMODE(child.stat().st_mode) & stat.S_IWGRP, child


def test_the_manager_asks_for_group_writable_modes_whatever_the_platform(
    worlds, monkeypatch
):
    """The companion to the POSIX test above, which cannot run on a Windows checkout:
    whatever the filesystem then does with it, the manager has to *ask* for a
    group-writable world on every file it writes."""
    import app.worlds as worlds_module

    calls: list[tuple[str, int]] = []
    real_chmod = worlds_module.os.chmod

    def recording_chmod(path, mode, *args, **kwargs):
        calls.append((str(path), mode))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(worlds_module.os, "chmod", recording_chmod)

    with TestClient(worlds["app"]) as client:
        login(client)
        assert (
            upload_world(
                client,
                [
                    ("Imported/_main.1.db2", b"data"),
                    ("Imported/_main.1.fwl2", b"meta"),
                    ("Imported/0_0.chunk", b"chunk"),
                ],
            ).status_code
            == 200
        )

    assert calls, "nothing had its mode set at all"
    for path, mode in calls:
        assert mode & stat.S_IWGRP, f"{path} was written without the group write bit"
    modes = {mode for _path, mode in calls}
    assert WORLD_FILE_MODE in modes
    # The directory gets setgid where the kernel allows it, and the plain mode is the
    # documented fallback; either way it is group-writable and group-searchable.
    assert modes & {WORLD_DIR_MODE, 0o775}


def test_a_failed_upload_leaves_no_staging_directory_behind(worlds):
    with TestClient(worlds["app"]) as client:
        login(client)
        assert upload_world(client, [("Lonely.db", b"data")]).status_code == 400
        assert upload_world(client, [("Imported/_main.1.db2", b"d")]).status_code == 400

    assert list(worlds["dir"].iterdir()) == []


# --------------------------------------------------------------- the surfaces


def test_the_panel_explains_switching_uploading_and_the_seed(worlds):
    with TestClient(worlds["app"]) as client:
        login(client)
        page = client.get("/").text

    assert "Worlds" in page
    words = " ".join(page.split())
    # What Load does, in the operator's terms.
    assert "picks the one the server" in words and "next time you start it" in words
    # What can be uploaded, spelled out in the panel itself.
    assert "the world's whole folder" in words
    assert ".zip" in page
    assert ".db" in page and ".fwl" in page
    # The legacy conversion warning, and the seed, which is why uploading exists.
    assert "permanently" in page
    assert "seed" in page
    # The cap, rendered from the value the manager will actually enforce.
    assert "2.0 MB" in page


def test_the_compose_file_mounts_the_game_volume_and_shares_the_game_group(project_root):
    """Two settings the feature cannot work without, neither of which is visible to a
    test that only monkeypatches the environment."""
    compose = (project_root / "docker-compose.yml").read_text(encoding="utf-8")
    manager_block = compose.split("  manager:", 1)[1]
    # Read-WRITE, spelled exactly: `valheim-config:/config:ro` contains the same
    # substring and would mount a volume no upload could ever be placed on.
    mounts = [
        line.strip().lstrip("- ").strip()
        for line in manager_block.splitlines()
        if "valheim-config:/config" in line
    ]
    assert mounts == ["valheim-config:/config"], mounts
    # The manager's group is the GAME's (PGID, 1000), not its own uid's.
    assert 'user: "${MANAGER_UID:-10001}:${MANAGER_GID:-1000}"' in manager_block
    for name in ("VALHEIM_WORLDS_DIR", "WORLD_UPLOAD_MAX_MB", "WORLD_UPLOAD_MAX_FILES"):
        assert f"{name}:" in manager_block, f"{name} is never passed to the manager"
    # Still nothing gained: the shared group is the alternative to a capability.
    assert "cap_drop: [ALL]" in manager_block


def test_the_managers_group_is_the_game_servers_own_group(project_root):
    """The ownership design in one assertion: the manager writes worlds group-writable
    and cannot chown, so its gid has to BE the gid the game server runs as. Editing
    PGID in the shipped settings without editing MANAGER_GID breaks that silently, and
    it surfaces as the server failing to save -- long after the upload said it worked.
    """
    compose = (project_root / "docker-compose.yml").read_text(encoding="utf-8")
    manager_block = compose.split("  manager:", 1)[1]
    manager_gid = re.search(r"MANAGER_GID:-(\d+)", manager_block).group(1)

    for path in (
        project_root / "valheim.env.example",
        project_root / "manager" / ".env.example",
    ):
        text = path.read_text(encoding="utf-8")
        if path.name == "valheim.env.example":
            game_gid = parse_env_text(text)["PGID"]
            assert game_gid == manager_gid, (
                f"the game server runs as PGID={game_gid} but the manager's default "
                f"group is {manager_gid}: uploaded worlds would not be writable by the "
                "server, which shows up as lost saves rather than as an error"
            )
        else:
            assert f"MANAGER_GID={manager_gid}" in text

    # And the same gid is what the manager writes into a fresh settings file.
    assert parse_env_text(DEFAULT_SETTINGS_TEXT)["PGID"] == manager_gid


def test_the_env_example_documents_the_shared_group_and_the_caps(project_root):
    example = (project_root / "manager" / ".env.example").read_text(encoding="utf-8")
    assert "MANAGER_GID=1000" in example
    assert "PGID" in example
    assert "WORLD_UPLOAD_MAX_MB=1024" in example
    assert "WORLD_UPLOAD_MAX_FILES=5000" in example


def test_the_readme_covers_switching_uploading_and_the_group(project_root):
    readme = (project_root / "README.md").read_text(encoding="utf-8")
    assert "Worlds: switching and adding new ones" in readme
    assert "MANAGER_GID" in readme and "PGID" in readme
    assert "seed" in readme
    assert "permanently" in readme
    assert "WORLD_UPLOAD_MAX_MB" in readme


def test_the_readme_leads_with_the_things_a_first_time_host_gets_wrong(project_root):
    """The README was rewritten for someone who has never used Docker, and the risk of
    that rewrite is losing a warning along with the jargon it was buried in. These are
    the four that cost real money or a real world if they go missing."""
    readme = (project_root / "README.md").read_text(encoding="utf-8")
    quick_start, _, rest = readme.partition("Advanced / Under the hood")
    assert rest, "the deep material should sit under an Advanced section"

    # Which ports to forward -- and which one NOT to.
    assert "2456" in quick_start and "2457" in quick_start and "2458" in quick_start
    assert "8080" in quick_start

    # A public host is reachable the moment it starts. This one is why the section
    # exists at all, so it belongs above the fold, not in the deep end.
    assert "MANAGER_BIND=127.0.0.1" in quick_start
    assert "VPS" in quick_start

    # No lockout, so the password length is the whole defence.
    assert "no lockout" in quick_start.lower()

    # The two passwords are not interchangeable, which is the commonest setup mistake.
    assert "Admin password" in quick_start and "Join password" in quick_start


# ------------------------------------------------- the module's own edge cases


@pytest.mark.parametrize(
    "raw",
    ["../escape", "a/b", "a\\b", "", "   ", ".", "..", ".hidden", "C:name", "x\x00y",
     "trailing.", "x" * 65],
)
def test_sanitised_name_refuses_everything_that_is_not_one_segment(raw):
    with pytest.raises(WorldError):
        sanitised_name(raw)


def test_a_world_name_is_stripped_rather_than_refused_for_its_whitespace():
    # Every other field in this manager is stripped; a name is not worth refusing over
    # a stray space the operator cannot see.
    assert sanitised_name("  Dedicated  ") == "Dedicated"


@pytest.mark.parametrize("raw", ["Dedicated", "Odin's Hall", "My World 2", "Åsgård"])
def test_sanitised_name_keeps_the_names_valheim_actually_allows(raw):
    # Spaces, apostrophes and Nordic letters are all real world names, so only what
    # stops a name being a single path segment is refused. The escapes are deliberate:
    # this literal was silently double-encoded once, and the round trip passed anyway.
    assert sanitised_name(raw) == raw


def test_human_size_states_the_unit_an_operator_would_use():
    assert human_size(512) == "512 B"
    assert human_size(2 * 1024 * 1024) == "2.0 MB"
    assert human_size(3 * 1024 * 1024 * 1024) == "3.0 GB"


# ------------------------------------------- the world knobs, read from the env
#
# `config_from_env` is where a deployed manager gets its limits, and nothing else in
# this suite goes through it: dropping the megabyte conversion below would enforce a
# 1024-BYTE cap on every real install while leaving the suite green.


def test_the_world_knobs_are_read_from_the_environment(monkeypatch):
    from app.main import config_from_env

    monkeypatch.setenv("VALHEIM_WORLDS_DIR", "/config/worlds_local")
    monkeypatch.setenv("WORLD_UPLOAD_MAX_MB", "32")
    monkeypatch.setenv("WORLD_UPLOAD_MAX_FILES", "1234")
    config = config_from_env()
    assert config.worlds_dir == "/config/worlds_local"
    # Megabytes in the env var, bytes in the config: the conversion is the whole
    # point of the setting, and getting it wrong is a 1 KB cap nobody could upload to.
    assert config.world_upload_max_bytes == 32 * 1024 * 1024
    assert config.world_upload_max_files == 1234


def test_the_world_knobs_have_the_documented_defaults(monkeypatch):
    from app.main import config_from_env

    for name in ("VALHEIM_WORLDS_DIR", "WORLD_UPLOAD_MAX_MB", "WORLD_UPLOAD_MAX_FILES"):
        monkeypatch.delenv(name, raising=False)
    config = config_from_env()
    assert config.worlds_dir == "/config/worlds_local"
    assert config.world_upload_max_bytes == 1024 * 1024 * 1024
    assert config.world_upload_max_files == 5000


def test_the_store_enforces_the_caps_the_page_states(env_file, fake_docker, worlds_dir):
    """One source for each limit. An injected store used to enforce its own numbers
    while the panel and the header gate quoted the config's."""
    config = build_config(
        env_file, worlds_dir=str(worlds_dir), world_upload_max_bytes=99 * 1024 * 1024
    )
    store = WorldStore(worlds_dir, max_upload_bytes=7 * 1024 * 1024, max_upload_files=11)
    app = create_app(
        config=config,
        controller=build_control(config, fake_docker),
        settings=SettingsStore(env_file),
        worlds=store,
    )
    with TestClient(app) as client:
        login(client)
        payload = client.get("/api/worlds").json()
        page = client.get("/").text

    assert payload["max_upload_bytes"] == 7 * 1024 * 1024
    assert payload["max_upload"] == "7.0 MB"
    assert payload["max_upload_files"] == 11
    # ...and the page quotes the same numbers, not the config's.
    assert "7.0 MB" in page and "99" not in page.split("Worlds")[-1].split("</table>")[0]
    assert 'data-max-bytes="7340032"' in page
    assert 'data-max-files="11"' in page


# --------------------------------------- the limits the documented route runs into


def test_a_folder_drop_of_more_than_a_thousand_files_is_accepted(worlds):
    """The documented primary route is dropping the world's folder, and a 1.0 world is
    one file per visited map chunk -- past the 1000 parts a multipart parser allows by
    default. Reverting that limit would silently break the feature's main path."""
    parts = [
        ("Sprawling/_main.1.db2", b"data"),
        ("Sprawling/_main.1.fwl2", b"meta"),
        ("Sprawling/_main.1.ok", b""),
    ]
    parts += [(f"Sprawling/{index}_0.chunk", b"c") for index in range(1050)]
    assert len(parts) > 1000

    with TestClient(worlds["app"]) as client:
        login(client)
        response = upload_world(client, parts)

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Sprawling"
    assert len(list((worlds["dir"] / "Sprawling").iterdir())) == len(parts)


def test_an_upload_past_the_file_limit_is_refused_pointing_at_the_zip(env_file, fake_docker, worlds_dir):
    config = build_config(env_file, worlds_dir=str(worlds_dir))
    app = create_app(
        config=config,
        controller=build_control(config, fake_docker),
        settings=SettingsStore(env_file),
        worlds=WorldStore(worlds_dir, max_upload_files=4),
    )
    parts = [("W/_main.1.db2", b"d"), ("W/_main.1.fwl2", b"m")]
    parts += [(f"W/{index}_0.chunk", b"c") for index in range(6)]

    with TestClient(app) as client:
        login(client)
        response = upload_world(client, parts)

    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert "zip" in error
    assert "Nothing was saved" in error
    # The standard refusal shape, not FastAPI's `detail`: Starlette turns the parser's
    # own exception into an HTTPException whenever an app is in the scope.
    assert "detail" not in response.json()
    assert response.json()["worlds"] == []
    assert list(worlds_dir.iterdir()) == []


def test_an_upload_that_does_not_declare_its_size_is_refused(worlds):
    """The size gate runs before the body is read, which it can only do if the size is
    declared. Letting an undeclared one through would spool it in full first."""
    with TestClient(worlds["app"]) as client:
        login(client)
        response = client.post(
            "/api/worlds/upload",
            headers={
                "Origin": ORIGIN,
                "Content-Type": "multipart/form-data; boundary=x",
                "Transfer-Encoding": "chunked",
            },
            content=iter([b"--x--\r\n"]),
        )

    assert response.status_code == 411, response.text
    assert "did not say how big" in response.json()["error"]
    assert list(worlds["dir"].iterdir()) == []


# ----------------------------------------------- never overwrite, not even a half


def test_a_lone_db_on_the_volume_still_blocks_an_upload_of_that_name(worlds):
    """The listing hides a half-world on purpose -- which made it exactly the thing an
    upload would rename straight over. The collision check reads the directory, not
    the list."""
    (worlds["dir"] / "Grandfathered.db").write_bytes(b"the only copy")

    with TestClient(worlds["app"]) as client:
        login(client)
        assert client.get("/api/worlds").json()["worlds"] == []
        response = upload_world(
            client, [("Grandfathered.db", b"new"), ("Grandfathered.fwl", b"new")]
        )

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert "Grandfathered" in error and "already taken" in error
    assert (worlds["dir"] / "Grandfathered.db").read_bytes() == b"the only copy"
    assert not (worlds["dir"] / "Grandfathered.fwl").exists()


@pytest.mark.parametrize(
    "existing, upload_as",
    [
        (["World.db", "world.fwl"], "World"),
        (["World.db", "world.fwl"], "world"),
        (["Dedicated.fwl"], "Dedicated"),
        (["Dedicated.fwl"], "dedicated"),
    ],
)
def test_a_case_mismatched_or_half_world_is_never_overwritten(worlds, existing, upload_as):
    for name in existing:
        (worlds["dir"] / name).write_bytes(b"the only copy")
    before = volume_snapshot(worlds["dir"])

    with TestClient(worlds["app"]) as client:
        login(client)
        response = upload_world(
            client,
            [(f"{upload_as}/_main.1.db2", b"new"), (f"{upload_as}/_main.1.fwl2", b"new")],
        )

    assert response.status_code == 409, response.text
    assert "already taken" in response.json()["error"]
    assert volume_snapshot(worlds["dir"]) == before


def test_a_directory_world_blocks_a_legacy_upload_of_the_same_name(worlds):
    make_modern_world(worlds["dir"], "Dedicated")
    before = volume_snapshot(worlds["dir"])

    with TestClient(worlds["app"]) as client:
        login(client)
        response = upload_world(client, [("Dedicated.db", b"new"), ("Dedicated.fwl", b"m")])

    assert response.status_code == 409, response.text
    assert volume_snapshot(worlds["dir"]) == before


def test_a_pair_beside_a_directory_of_the_same_name_is_one_row_not_two(worlds):
    """Two rows would both be marked active, and the collision check would see one."""
    make_modern_world(worlds["dir"], "Dedicated")
    make_legacy_world(worlds["dir"], "Dedicated")

    with TestClient(worlds["app"]) as client:
        login(client)
        payload = client.get("/api/worlds").json()

    assert [row["name"] for row in payload["worlds"]] == ["Dedicated"]
    assert payload["worlds"][0]["layout"] == "1.0"
    assert len([row for row in payload["worlds"] if row["active"]]) == 1


def test_a_pair_spelled_with_different_cases_is_listed_as_one_world(worlds):
    (worlds["dir"] / "Grandfathered.db").write_bytes(b"data")
    (worlds["dir"] / "grandfathered.fwl").write_bytes(b"meta")

    with TestClient(worlds["app"]) as client:
        login(client)
        payload = client.get("/api/worlds").json()

    # The `.db` holds the save, so its spelling names the world.
    assert [row["name"] for row in payload["worlds"]] == ["Grandfathered"]
    assert payload["worlds"][0]["layout"] == "legacy"


def test_a_pair_uploaded_with_different_cases_is_accepted_as_one_world(worlds):
    with TestClient(worlds["app"]) as client:
        login(client)
        response = upload_world(client, [("World.db", b"data"), ("world.fwl", b"meta")])

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "World"
    assert (worlds["dir"] / "World.db").read_bytes() == b"data"
    assert (worlds["dir"] / "World.fwl").read_bytes() == b"meta"


# ------------------------------------------------ the name an upload is stored as


def test_a_legacy_pair_can_be_uploaded_under_a_different_name(worlds):
    """The pair carries its old name in its own file names, and the new one is where
    it lands -- two different strings that a swap would quietly transpose, writing
    under the wrong name while reporting the right one."""
    with TestClient(worlds["app"]) as client:
        login(client)
        response = upload_world(
            client,
            [("Grandfathered.db", b"data"), ("Grandfathered.fwl", b"meta")],
            name="Renamed",
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["name"] == "Renamed"
    assert payload["layout"] == "legacy"
    assert (worlds["dir"] / "Renamed.db").read_bytes() == b"data"
    assert (worlds["dir"] / "Renamed.fwl").read_bytes() == b"meta"
    # The name it arrived under is gone entirely, including from the listing.
    assert not (worlds["dir"] / "Grandfathered.db").exists()
    assert not (worlds["dir"] / "Grandfathered.fwl").exists()
    assert [row["name"] for row in payload["worlds"]] == ["Renamed"]
    # And it is switchable under the new name, which is the point of the rename.
    with TestClient(worlds["app"]) as client:
        login(client)
        assert switch_world(client, "Renamed").status_code == 200
    assert "WORLD_NAME=Renamed" in worlds["env"].read_text(encoding="utf-8")


def test_a_zip_in_a_folder_does_not_suggest_the_folder_as_the_name(worlds):
    """A browser part is named with its path; only the leaf names the world. A flat
    archive is where this bit -- `Backups/Imported.zip` suggested `Backups/Imported`,
    a name the manager then refused for a slash the operator never typed."""
    archive = zip_bytes([("_main.1.db2", b"data"), ("_main.1.fwl2", b"meta")])

    with TestClient(worlds["app"]) as client:
        login(client)
        response = upload_world(client, [("Backups/Imported.zip", archive)])

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Imported"


# ----------------------------------------------- a world the switch would refuse


@pytest.mark.parametrize("name", ["colon:name", "x" * 65])
def test_a_world_the_switch_would_refuse_is_listed_but_not_offered(worlds, name):
    """Listing a Load button the switch then rejects, for a name the operator never
    typed, is the worst of both answers."""
    try:
        make_modern_world(worlds["dir"], name)
    except OSError:
        pytest.skip("this filesystem will not create a world directory with that name")

    with TestClient(worlds["app"]) as client:
        login(client)
        payload = client.get("/api/worlds").json()
        refused = switch_world(client, name)

    row = next(row for row in payload["worlds"] if row["name"] == name)
    assert row["loadable"] is False
    assert row["unloadable"]
    # ...and the switch says the same thing rather than a surprise.
    assert refused.status_code == 400


def test_a_normal_world_is_loadable(worlds):
    make_modern_world(worlds["dir"], "Dedicated")
    with TestClient(worlds["app"]) as client:
        login(client)
        payload = client.get("/api/worlds").json()

    assert payload["worlds"][0]["loadable"] is True
    assert payload["worlds"][0]["unloadable"] == ""


# ----------------------------------------------------- refusals reach the log


def test_every_upload_refusal_is_logged_on_the_host(worlds, caplog):
    """One JSON error in one browser was the whole trace a rejected traversal entry or
    a run of oversize attempts left behind."""
    archive = zip_bytes([("W/_main.1.db2", b"d"), ("../escape", b"owned")])
    with caplog.at_level(logging.WARNING, logger="valheim_manager"):
        with TestClient(worlds["app"]) as client:
            login(client)
            assert upload_world(client, [("W.zip", archive)]).status_code == 400
            assert upload_world(client, [("Lonely.db", b"d")]).status_code == 400

    logged = [record.getMessage() for record in caplog.records if "refused" in record.getMessage()]
    assert len(logged) == 2, logged
    assert any("outside the world directory" in line for line in logged)


# =====================================================================
# The tabbed dashboard: Console, Server settings, Worlds.
#
# The panels themselves are unchanged -- every assertion above about the settings
# help text, the modifier controls and the worlds table still holds. What is pinned
# here is the wrapping: that all three panels are rendered on every load (hiding is
# what keeps the console's buffer and the WebSocket alive, so nothing may be built
# on demand), that the Console is the one showing, and that the tab wiring the
# keyboard and the screen reader depend on is internally consistent.
# =====================================================================

PANEL_IDS = ["panel-console", "panel-settings", "panel-worlds"]

TAB_BUTTON_RE = re.compile(r"<button[^>]*\brole=\"tab\"[^>]*>", re.S)


def _tab_attributes(page: str) -> list[dict[str, str]]:
    """Every ``role="tab"`` button's attributes, in document order."""
    tabs = []
    for match in TAB_BUTTON_RE.finditer(page):
        tabs.append(dict(re.findall(r"([a-z-]+)=\"([^\"]*)\"", match.group(0))))
    return tabs


def _panels(page: str) -> dict[str, str]:
    """The markup of each tab panel, sliced at the next panel's opening tag."""
    marks = []
    for panel_id in PANEL_IDS:
        needle = f'id="{panel_id}"'
        assert needle in page, f"{panel_id} is not rendered"
        marks.append(page.index(needle))
    assert marks == sorted(marks), "the panels are not in Console/settings/worlds order"
    marks.append(len(page))
    return {PANEL_IDS[i]: page[marks[i] : marks[i + 1]] for i in range(len(PANEL_IDS))}


def test_the_dashboard_lands_on_the_console_tab(stack):
    """First load, no stored choice: the markup itself has to open on the Console, or
    the landing tab would depend on a script that has not run yet."""
    with TestClient(stack["app"]) as client:
        login(client)
        page = client.get("/").text

    tabs = _tab_attributes(page)
    assert [tab["data-tab"] for tab in tabs] == ["console", "settings", "worlds"]

    selected = [tab for tab in tabs if tab["aria-selected"] == "true"]
    assert len(selected) == 1, "exactly one tab may be selected"
    assert selected[0]["data-tab"] == "console"
    assert selected[0]["aria-controls"] == "panel-console"

    # ...and the Console panel is the only one showing.
    panels = _panels(page)
    assert "hidden" not in panels["panel-console"].split(">")[0]
    for panel_id in ("panel-settings", "panel-worlds"):
        assert "hidden" in panels[panel_id].split(">")[0], panel_id


def test_the_tab_wiring_is_internally_consistent(stack):
    """`aria-controls` pointing at nothing is a tab that opens a blank page, and two
    tab stops in the strip is a keyboard trap for the arrow keys."""
    with TestClient(stack["app"]) as client:
        login(client)
        page = client.get("/").text

    assert 'role="tablist"' in page
    tabs = _tab_attributes(page)
    assert len(tabs) == len(PANEL_IDS)

    for tab in tabs:
        assert tab["aria-controls"] in PANEL_IDS
        assert f'id="{tab["aria-controls"]}"' in page
        # THIS element's opening tag, not "somewhere later in the document" -- sliced
        # to EOF, any other tabpanel on the page would have satisfied it.
        opening = page[page.index(f'id="{tab["aria-controls"]}"') :]
        opening = opening[: opening.index(">")]
        assert 'role="tabpanel"' in opening, tab["aria-controls"]
        assert 'tabindex="0"' in opening, tab["aria-controls"]
        # One tab stop for the whole strip: only the selected tab is in the tab order.
        assert tab["tabindex"] == ("0" if tab["aria-selected"] == "true" else "-1")
        # Each panel names its tab back, so the panel has an accessible name.
        assert f'aria-labelledby="{tab["id"]}"' in page

    # Every panel is pointed at by exactly one tab.
    assert sorted(tab["aria-controls"] for tab in tabs) == sorted(PANEL_IDS)


def test_all_three_panels_are_rendered_on_every_load(stack):
    """Hidden, never absent -- the precondition for the console surviving a switch.

    That it actually survives is not checked here and cannot be: this reads markup.
    ``test_tabs_runtime.py`` executes the page and checks the survival itself.
    """
    with TestClient(stack["app"]) as client:
        login(client)
        page = client.get("/").text

    panels = _panels(page)

    # The Console tab: the status badge is in the strip (visible from every tab), and
    # the controls and the log live in the panel.
    assert page.index('id="status-badge"') < page.index('id="panel-console"')
    for needle in ('id="btn-start"', 'id="btn-stop"', 'id="btn-restart"', 'id="console"'):
        assert needle in panels["panel-console"], needle

    # The settings panel, with the help text and the editor the panel tests assert.
    for needle in ('id="settings-table"', 'id="settings-form"', 'id="btn-settings-edit"'):
        assert needle in panels["panel-settings"], needle
    assert "new settings take effect" in " ".join(panels["panel-settings"].split())

    # The worlds panel, with its table and its upload form.
    for needle in ('id="worlds-table"', 'id="world-upload-form"', 'id="world-dropzone"'):
        assert needle in panels["panel-worlds"], needle

    # And nothing leaked across: a control in two panels is a duplicate id.
    assert 'id="btn-start"' not in panels["panel-settings"] + panels["panel-worlds"]
    assert 'id="settings-form"' not in panels["panel-console"] + panels["panel-worlds"]


def test_every_panel_is_pinned_hidden_by_its_own_rule(stack):
    """An author `display` beats the UA stylesheet's `[hidden] { display: none }`, and
    that exact bug shipped here once already -- with three panels it would stack the
    whole dashboard down the page instead of hiding one editor."""
    css = (Path(__file__).resolve().parents[1] / "static" / "style.css").read_text(
        encoding="utf-8"
    )
    collapsed = " ".join(css.split())
    for panel_id in PANEL_IDS:
        assert f"#{panel_id}[hidden] {{ display: none; }}" in collapsed, panel_id

    # ...and nothing later in the file undoes them. The guards being PRESENT says
    # nothing about their winning: a later `#panel-settings { display: block }`, or
    # any `display` marked !important, walks straight past them and stacks the whole
    # dashboard down the page again. Positions come from the match, not from a search
    # for the selector text -- every panel's name also occurs in its own guard.
    guards_end = max(collapsed.index(f"#{pid}[hidden]") for pid in PANEL_IDS)
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", collapsed):
        selector, body = match.group(1).strip(), match.group(2)
        if "display" not in body:
            continue
        assert "!important" not in body, f"a !important display outranks the guards: {selector}"
        if match.start() <= guards_end:
            continue
        touches_panel = "tabpanel" in selector or any(pid in selector for pid in PANEL_IDS)
        assert not touches_panel, f"{selector} sets display on a panel after its guard"


# =====================================================================
# Deleting a world, and making a new one.
#
# Delete is the only operation in this project that destroys data, so what it
# refuses matters more than what it does: the world the server is set to load, a
# name that is not a single path segment, a symlink, and anything at all while the
# server is up. "New world" is the switch with the collision check inverted -- the
# manager cannot generate a save, so all it can do is point WORLD_NAME at a name
# nothing is using and let the next Start build it.
# =====================================================================


def delete_world(client: TestClient, name: str, *, origin: str = ORIGIN):
    return client.post(
        "/api/worlds/delete", json={"name": name}, headers={"Origin": origin}
    )


def new_world(client: TestClient, name: str, *, origin: str = ORIGIN):
    return client.post(
        "/api/worlds/new", json={"name": name}, headers={"Origin": origin}
    )


def test_deleting_a_world_removes_its_whole_directory(worlds):
    make_modern_world(worlds["dir"], "Doomed", chunks=4)
    make_modern_world(worlds["dir"], "Keeper")
    worlds["docker"].seed_stopped()

    with TestClient(worlds["app"]) as client:
        login(client)
        response = delete_world(client, "Doomed")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["deleted"] is True
    assert "Doomed" in payload["message"]
    assert not (worlds["dir"] / "Doomed").exists()
    # ...and only that world.
    assert (worlds["dir"] / "Keeper").is_dir()
    assert [world["name"] for world in payload["worlds"]] == ["Keeper"]


def test_deleting_a_pre_1_0_world_takes_both_halves(worlds):
    make_legacy_world(worlds["dir"], "Oldsave")
    worlds["docker"].seed_stopped()

    with TestClient(worlds["app"]) as client:
        login(client)
        assert delete_world(client, "Oldsave").status_code == 200

    # A .db left behind would be invisible in the list and still block an upload of
    # the same name -- the worst of both outcomes.
    assert not (worlds["dir"] / "Oldsave.db").exists()
    assert not (worlds["dir"] / "Oldsave.fwl").exists()


def test_deleting_takes_a_half_world_the_listing_never_showed(worlds):
    """A lone .db is not listed as a world, is in the way of an upload, and is exactly
    what someone would be trying to clear out."""
    (worlds["dir"] / "Ghost.db").write_bytes(b"orphan")
    worlds["docker"].seed_stopped()

    with TestClient(worlds["app"]) as client:
        login(client)
        listed = client.get("/api/worlds").json()
        assert [world["name"] for world in listed["worlds"]] == []
        assert delete_world(client, "Ghost").status_code == 200

    assert not (worlds["dir"] / "Ghost.db").exists()


def test_deleting_the_world_the_server_loads_is_refused(worlds):
    make_modern_world(worlds["dir"], "Dedicated")
    worlds["docker"].seed_stopped()
    before = volume_snapshot(worlds["dir"])

    with TestClient(worlds["app"]) as client:
        login(client)
        response = delete_world(client, "Dedicated")

    assert response.status_code == 409
    error = response.json()["error"]
    assert "set to load" in error
    # The refusal has to say what to do instead, or it is a dead end.
    assert "Load a different world" in error or "make a new one" in error
    assert volume_snapshot(worlds["dir"]) == before


@pytest.mark.parametrize("state", ["running", "restarting", "paused"])
def test_deleting_is_refused_while_the_server_is_live(worlds, state):
    make_modern_world(worlds["dir"], "Seedy")
    container = worlds["docker"].seed_stopped()
    container.status = state
    before = volume_snapshot(worlds["dir"])

    with TestClient(worlds["app"]) as client:
        login(client)
        response = delete_world(client, "Seedy")

    assert response.status_code == 409
    assert "Turn the server off first" in response.json()["error"]
    assert volume_snapshot(worlds["dir"]) == before


@pytest.mark.parametrize("raw", ["../escape", "a/b", "a\\b", "", "   ", ".", "..", "C:name"])
def test_deleting_refuses_a_name_that_is_not_one_world(worlds, raw):
    make_modern_world(worlds["dir"], "Seedy")
    worlds["docker"].seed_stopped()
    before = volume_snapshot(worlds["dir"])

    with TestClient(worlds["app"]) as client:
        login(client)
        response = delete_world(client, raw)

    assert response.status_code == 400, response.text
    assert volume_snapshot(worlds["dir"]) == before


def test_deleting_a_world_that_is_not_there_says_so(worlds):
    worlds["docker"].seed_stopped()
    with TestClient(worlds["app"]) as client:
        login(client)
        response = delete_world(client, "Ghost")

    assert response.status_code == 400
    assert "no world called" in response.json()["error"]


@POSIX_ONLY
def test_deleting_refuses_a_link_rather_than_following_it(worlds, tmp_path):
    """A link in worlds_local pointing at something else on the host is not a world,
    and rmtree through it would take the target with it."""
    outside = tmp_path / "not-a-world"
    outside.mkdir()
    (outside / "keepme").write_bytes(b"important")
    (worlds["dir"] / "Linked").symlink_to(outside, target_is_directory=True)
    worlds["docker"].seed_stopped()

    with TestClient(worlds["app"]) as client:
        login(client)
        response = delete_world(client, "Linked")

    assert response.status_code == 400
    assert "link" in response.json()["error"]
    assert (outside / "keepme").exists()


def test_deleting_needs_the_origin_header(worlds):
    make_modern_world(worlds["dir"], "Seedy")
    worlds["docker"].seed_stopped()
    before = volume_snapshot(worlds["dir"])

    with TestClient(worlds["app"]) as client:
        login(client)
        response = client.post("/api/worlds/delete", json={"name": "Seedy"})

    assert response.status_code == 403
    assert volume_snapshot(worlds["dir"]) == before


# ------------------------------------------------------------------ new world


def test_a_new_world_points_the_server_at_a_name_nothing_is_using(worlds):
    container = worlds["docker"].seed_stopped()

    with TestClient(worlds["app"]) as client:
        login(client)
        response = new_world(client, "Secondworld")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["saved"] is True
    assert "WORLD_NAME=Secondworld" in worlds["env"].read_text(encoding="utf-8")
    # The same step that makes any settings change real.
    assert container.removed is True
    # Nothing was created on the volume: Valheim builds the world, not the manager.
    assert not (worlds["dir"] / "Secondworld").exists()
    assert "Press Start" in payload["message"]


def test_a_new_world_refuses_a_name_that_already_exists(worlds):
    make_modern_world(worlds["dir"], "Seedy")
    worlds["docker"].seed_stopped()
    before = worlds["env"].read_text(encoding="utf-8")

    with TestClient(worlds["app"]) as client:
        login(client)
        response = new_world(client, "Seedy")

    assert response.status_code == 409
    error = response.json()["error"]
    assert "already a world called Seedy" in error
    # Saying "press Load instead" is the difference between a refusal and a dead end.
    assert "Load" in error
    assert worlds["env"].read_text(encoding="utf-8") == before


def test_a_new_world_refuses_a_half_world_the_listing_never_showed(worlds):
    """A lone .db is invisible in the list, and the next Start would adopt it under
    this name -- which is the one thing "new world" must never do."""
    (worlds["dir"] / "Ghost.db").write_bytes(b"orphan")
    worlds["docker"].seed_stopped()

    with TestClient(worlds["app"]) as client:
        login(client)
        response = new_world(client, "Ghost")

    assert response.status_code == 409
    assert "Ghost.db" in response.json()["error"]


@pytest.mark.parametrize("state", ["running", "restarting", "paused"])
def test_making_a_new_world_is_refused_while_the_server_is_live(worlds, state):
    container = worlds["docker"].seed_stopped()
    container.status = state
    before = worlds["env"].read_text(encoding="utf-8")

    with TestClient(worlds["app"]) as client:
        login(client)
        response = new_world(client, "Secondworld")

    assert response.status_code == 409
    assert "Turn the server off first" in response.json()["error"]
    assert worlds["env"].read_text(encoding="utf-8") == before


@pytest.mark.parametrize("raw", ["../escape", "a/b", "", "   ", "..", "x" * 65])
def test_a_new_world_refuses_a_name_that_is_not_one_world(worlds, raw):
    worlds["docker"].seed_stopped()
    before = worlds["env"].read_text(encoding="utf-8")

    with TestClient(worlds["app"]) as client:
        login(client)
        response = new_world(client, raw)

    assert response.status_code == 400, response.text
    assert worlds["env"].read_text(encoding="utf-8") == before


def test_the_worlds_panel_offers_delete_and_a_way_to_make_a_new_world(worlds):
    with TestClient(worlds["app"]) as client:
        login(client)
        page = client.get("/").text

    assert 'id="world-new-form"' in page and 'id="btn-world-new"' in page
    # The confirmation is a real dialog, and Cancel is what the keyboard lands on --
    # the destructive button must never be the default.
    assert 'id="delete-dialog"' in page
    dialog = page[page.index('id="delete-dialog"') :]
    dialog = dialog[: dialog.index("</dialog>")]
    assert 'id="btn-delete-confirm"' in dialog and 'id="btn-delete-cancel"' in dialog
    assert "autofocus" in dialog[: dialog.index('id="btn-delete-confirm"')]
    assert "cannot bring it back" in " ".join(dialog.split())


@pytest.mark.parametrize("raw", ["../escape", "a/b", "a\b", "..", ".", "", "   ", ".hidden"])
def test_the_store_refuses_to_delete_anything_that_is_not_one_world(worlds_dir, raw):
    """The route sanitises the name before the store sees it, so this guard has no
    caller that can currently reach it -- which is exactly why it needs its own test.
    The next caller (deleting a world's mods along with it, say) would otherwise be the
    one to find out it was never load-bearing.

    What it buys is the MESSAGE, not the safety. Escaping is already impossible without
    it: ``blocking_entries`` only ever matches the bare names of direct children, so
    ``../escape`` matches nothing and the delete does nothing. But it would then report
    "there is no world called '../escape'", which describes the wrong problem. So the
    assertion here is that the refusal talks about the name.
    """
    make_modern_world(worlds_dir, "Seedy")
    store = WorldStore(worlds_dir)

    with pytest.raises(WorldError) as caught:
        store.delete(raw)

    assert "no world called" not in str(caught.value), (
        f"{raw!r} was treated as a world that happens to be missing, rather than as a "
        f"name that is not a world: {caught.value}"
    )
    assert (worlds_dir / "Seedy").is_dir()


def test_the_store_never_deletes_outside_its_own_directory(worlds_dir, tmp_path):
    """`../` is not a world name, and worlds_local sits inside a volume with other
    things in it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keepme").write_bytes(b"important")
    store = WorldStore(worlds_dir)

    with pytest.raises(WorldError):
        store.delete("../outside")

    assert (outside / "keepme").exists()
    assert outside.is_dir()


# =====================================================================
# Preparing the worlds folder.
#
# The game server creates /config/worlds_local as PUID:PGID with umask 022, so
# mode 755. The manager runs as its own uid in the game's group and cannot chmod
# what it does not own -- it is not the owner and cap_drop: [ALL] takes CAP_FOWNER.
# So every upload and every delete failed with EACCES until somebody ran chmod on
# the host by hand, which is the one thing this project is supposed to spare them.
# =====================================================================


def _compose_block(project_root, service: str) -> str:
    compose = (project_root / "docker-compose.yml").read_text(encoding="utf-8")
    block = compose.split(f"  {service}:", 1)[1]
    following = re.search(r"\n  [a-z][a-z0-9_-]*:\n", block)
    return block[: following.start()] if following else block


def test_the_permissions_service_prepares_the_worlds_folder(project_root):
    block = _compose_block(project_root, "permissions")

    # Root, because chmod on someone else's files needs it -- and in a container of
    # its own precisely so the long-running, network-facing manager never is.
    assert 'user: "0:0"' in block
    assert 'network_mode: "none"' in block, "the one root container should reach nothing"
    assert "valheim-config:/config" in block
    assert 'restart: "no"' in block, "a one-shot that restarts is a boot loop"

    # What it actually does: the directory group-writable and setgid, and the world
    # files inside it group-writable too -- deleting a world needs write on the
    # world's own directory, not just on the folder holding it.
    assert "/config/worlds_local" in block
    assert "chmod 2775" in block
    assert "chmod 664" in block
    assert "chgrp" in block

    # It must not be able to take the dashboard down with it. The manager waits for
    # this to COMPLETE, so a non-zero exit would stop the manager starting at all --
    # turning "uploads are refused" into "nothing runs".
    assert "exit 0" in block
    assert "WARNING" in block, "a failure has to say so somewhere"


def test_the_fixer_escapes_its_shell_variables(project_root):
    """Compose interpolates `$` in this file. An unescaped $dir is substituted away
    to an empty string before sh ever sees it, and the script then silently operates
    on nothing -- the exact failure mode this project already hit once with bcrypt
    hashes in an env file."""
    block = _compose_block(project_root, "permissions")
    script = block.split("command:", 1)[1]

    assert "$$dir" in script and "$$gid" in script
    # Every `$` in the script is either an escaped `$$` or a `${...}` Compose is
    # meant to substitute. A bare `$name` is the bug.
    bare = re.findall(r"(?<!\$)\$(?!\$|\{)[A-Za-z_(]", script.replace("$$", ""))
    assert bare == [], f"unescaped shell variables in the fixer: {bare}"


def test_the_manager_waits_for_the_worlds_folder_to_be_prepared(project_root):
    """Ordering is not enough on its own: an upload accepted before the fix ran would
    fail for a reason that had already been solved."""
    block = _compose_block(project_root, "manager")
    assert "permissions:" in block
    assert "condition: service_completed_successfully" in block


def test_the_game_is_asked_to_create_group_writable_files(project_root):
    """The fixer repairs what is already on the volume; this is what stops the problem
    coming back every time the game server creates a world."""
    assert parse_env_text(DEFAULT_SETTINGS_TEXT)["PERMISSIONS_UMASK"] == "002"
    example = (project_root / "valheim.env.example").read_text(encoding="utf-8")
    assert parse_env_text(example)["PERMISSIONS_UMASK"] == "002"


def test_the_umask_is_not_something_the_panel_can_quietly_change(project_root):
    """It is not one of the panel's keys, so a save leaves it exactly as written --
    which is what keeps a settings edit from re-breaking uploads."""
    from app.setup import SETTINGS_KEYS

    assert "PERMISSIONS_UMASK" not in SETTINGS_KEYS


def test_the_game_container_gets_a_group_writable_umask(stack, env_file):
    """An install made before PERMISSIONS_UMASK existed has no such key, and the
    manager never adds keys to a file the operator owns -- so the default is applied
    at create time instead. Without it the game keeps making worlds the manager
    cannot delete, and the one-shot fixer would have work to do after every Start."""
    assert "PERMISSIONS_UMASK" not in env_file.read_text(encoding="utf-8")

    stack["control"].start()

    created = stack["docker"].containers.create_calls[0]
    assert created["environment"]["PERMISSIONS_UMASK"] == "002"


def test_a_umask_the_operator_set_themselves_is_left_alone(stack, env_file):
    """It is a default, not an override. Someone who deliberately wants 022 gets it."""
    env_file.write_text(
        env_file.read_text(encoding="utf-8") + "\nPERMISSIONS_UMASK=027\n", encoding="utf-8"
    )

    stack["control"].start()

    created = stack["docker"].containers.create_calls[0]
    assert created["environment"]["PERMISSIONS_UMASK"] == "027"


def test_creating_a_container_does_not_modify_the_settings_it_was_given(env_file, fake_docker):
    """The umask default belongs to the container, not to the caller's dictionary.
    `container_env` builds a fresh dict today and so nothing would notice -- but it is
    a provider callable, and a memoised one would start handing out a key the operator
    never set, which would then show up as a settings row and as a diff on save."""
    config = build_config(env_file)
    shared = SettingsStore(env_file).container_env()
    control = DockerControl(
        base_url=config.docker_host,
        container_name=config.container_name,
        image=config.image,
        network=config.network,
        config_volume=config.config_volume,
        data_volume=config.data_volume,
        env_provider=lambda: shared,
        port_provider=lambda: [2456, 2457, 2458],
        ready_pattern=config.ready_pattern,
        stop_timeout=config.stop_timeout,
        restart_policy=config.restart_policy,
        client_factory=lambda: fake_docker,
    )

    control.start()

    assert "PERMISSIONS_UMASK" not in shared, "the provider's own dict was modified"
    created = fake_docker.containers.create_calls[0]
    assert created["environment"]["PERMISSIONS_UMASK"] == "002"


def test_the_umask_default_does_not_leak_into_the_settings_the_panel_shows(stack, env_file):
    """The container gets the default; the operator's file does not silently grow a
    key they never wrote."""
    stack["control"].start()

    assert "PERMISSIONS_UMASK" not in env_file.read_text(encoding="utf-8")


# =====================================================================
# The words the panels use.
#
# Every explanation on this page is read by someone who wants to play Valheim with
# their friends, not by someone administering Docker. The vocabulary below kept
# creeping back in because each sentence was individually true -- a save really does
# remove a container -- so the rule is pinned here rather than left to taste.
# =====================================================================

# Docker's vocabulary, and the manager's own file layout. True, and none of it is the
# operator's problem: they turn the server off, change a setting, and press Start.
JARGON = [
    "container",
    "environment",
    "volume",
    "SERVER_ARGS",
    "WORLD_NAME",
    "/config",
    "valheim.env",
    "Content-Length",
]

# The Status card's readout is exempt: `Name`, `Game server`, `State` and `Started`
# are labelled facts an operator needs when something is wrong (the name is what a
# `docker rm` takes), not an explanation of how any of it works.
PROSE_RE = re.compile(r'<p[^>]*class="[^"]*muted[^"]*"[^>]*>(.*?)</p>', re.S)
# Explanations that only appear on hover or focus are still explanations, and moving
# one into a bubble must not move it out of this check.
HINT_RE = re.compile(r'<span[^>]*class="[^"]*hint-bubble[^"]*"[^>]*>(.*?)</span>', re.S)


def _panel_prose(page: str, panel_id: str) -> str:
    start = page.index(f'id="{panel_id}"')
    rest = page[start:]
    end = rest.find('<div id="panel-', 1)
    panel = rest if end == -1 else rest[:end]
    blocks = PROSE_RE.findall(panel) + HINT_RE.findall(panel)
    sentences = [re.sub(r"<[^>]+>", " ", block) for block in blocks]
    return " ".join(" ".join(sentences).split())


def test_every_setting_is_named_the_same_way_wherever_it_appears(stack, env_file):
    """The editor called it "Join password" and the table beside it called the same row
    SERVER_PASS. One setting, two names, depending on whether you were reading it or
    changing it."""
    env_file.write_text(
        env_file.read_text(encoding="utf-8") + "\nMY_OWN_KEY=42\n", encoding="utf-8"
    )
    with TestClient(stack["app"]) as client:
        login(client)
        status = client.get("/api/status").json()
        page = client.get("/").text

    labels = {row["key"]: row["label"] for row in status["settings"]}
    assert labels["SERVER_NAME"] == "Server name"
    assert labels["SERVER_PASS"] == "Join password"
    assert labels["SERVER_PORT"] == "Game port"
    # A key the manager has no name for keeps its own: it is the only name it has, and
    # inventing one for something the operator added by hand would be worse.
    assert labels["MY_OWN_KEY"] == "MY_OWN_KEY"

    # The editor's own labels, which the table now matches rather than contradicts.
    words = " ".join(page.split())
    for label in ("Server name", "World name", "Join password"):
        assert label in words, label


def test_the_panels_explain_themselves_without_docker_vocabulary(stack):
    """What the operator reads, in words about their game server."""
    with TestClient(stack["app"]) as client:
        login(client)
        page = client.get("/").text

    for panel_id in ("panel-settings", "panel-worlds"):
        prose = _panel_prose(page, panel_id)
        assert prose, f"no explanatory text found in {panel_id}"
        for word in JARGON:
            assert word.lower() not in prose.lower(), f"{panel_id} says {word!r}: {prose}"


def test_the_panels_still_say_the_thing_the_jargon_was_carrying(stack):
    """Plainer, not vaguer. Each sentence the rewrite replaced was load-bearing, and
    dropping the fact along with the vocabulary is the way this goes wrong."""
    with TestClient(stack["app"]) as client:
        login(client)
        page = client.get("/").text

    settings = _panel_prose(page, "panel-settings")
    # A saved value does nothing until the server is restarted by hand.
    assert "Turn the server off to change them" in settings
    assert "new settings take effect" in settings
    # ...and saving is not going to cost anyone their world.
    assert "never touched" in settings
    # A world name nobody has used before means a NEW world, which is the one way to
    # lose track of a save game from this panel.
    assert "brand-new" in settings and "old world is kept" in settings

    worlds = _panel_prose(page, "panel-worlds")
    # Load needs the server off, and takes effect on the next start.
    assert "next time you start it" in worlds
    assert "server has to be off" in worlds
    # Uploading is the only way to choose a seed.
    assert "seed" in worlds
    # An upload never silently replaces a world that is already there.
    assert "never written over" in worlds


def test_a_refusal_tells_the_operator_what_to_do_about_it(stack):
    """A refusal that only states a fact leaves the operator stuck."""
    stack["docker"].seed_running()
    with TestClient(stack["app"]) as client:
        login(client)
        refused = save_settings(client, {"SERVER_NAME": "Asgard"})

    assert refused.status_code == 409, refused.text
    error = refused.json()["error"]
    assert "Turn the server off first" in error
    assert "container" not in error.lower()


def test_tab_switching_never_calls_the_manager(stack):
    """Lock state, error text and panel contents all come from the existing status
    push; a tab switch that fetched anything would be a second source of truth.

    A cheap tripwire over the source, not a behavioural check -- ``test_tabs_runtime``
    records the real requests and the real socket. Two things this used to get wrong,
    and both mattered: it stopped scanning at ``initTabs``, so the click handler and
    the restore path -- the two routes a switch actually takes -- were outside the
    window entirely; and it named ``fetch(``/``XMLHttpRequest`` while this file's own
    way of reaching the manager is ``post(`` and ``refreshWorlds()``.
    """
    js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    # The whole tabs section, banner to banner: selection, the keyboard, and init.
    start = js.index("------- tabs")
    body = js[start : js.index("---------- console", start)]
    assert "function initTabs(" in body, "the scan window no longer covers init"
    assert "function selectTab(" in body, "the scan window no longer covers selection"

    # The comments here talk about the socket and about not fetching; only code counts.
    code = re.sub("//.*", "", body)
    for forbidden in ("fetch(", "XMLHttpRequest", "post(", "refreshWorlds(", "socket",
                      "innerHTML", "replaceChildren"):
        assert forbidden not in code, forbidden
    # Shown and hidden, never rebuilt.
    assert ".hidden = !on" in code
