"""FastAPI app: first-run setup, login gate, server controls, and the log WebSocket.

Route map
---------
``GET  /``             dashboard, or 302 to /login when unauthenticated
``GET  /login``        login form
``POST /login``        verify credentials, set the session cookie
``POST /logout``       clear the session cookie
``GET  /setup``        first-run wizard -- only while unconfigured, only with the token
``POST /setup``        create the admin, generate the session secret, write settings
``GET  /api/status``   status + masked current settings (auth required)
``POST /api/start``    pull/create/start           (auth + origin check)
``POST /api/stop``     graceful stop, ``{"force": true}`` to kill (auth + origin check)
``POST /api/restart``  stop then start             (auth + origin check)
``POST /api/settings`` save edited settings, only while off (auth + origin check)
``GET  /api/worlds``   the worlds on the game volume, active one marked (auth required)
``POST /api/worlds/switch``  point WORLD_NAME at one, only while off (auth + origin)
``POST /api/worlds/upload``  multipart world import, only while off (auth + origin)
``WS   /ws/logs``      status pushes + live log lines (auth required)
``GET  /healthz``      unauthenticated liveness probe, leaks nothing

Two boot paths
--------------
*Configured* -- credentials come from ``ADMIN_USER``/``ADMIN_PASSWORD_HASH``/
``SESSION_SECRET`` in the environment, or from the state file the wizard wrote. A bad
hash or a short secret still refuses to boot, loudly, exactly as before; so does a
state file that exists but cannot be read as a complete document, because falling
back to "unconfigured" there would reopen admin creation on a live manager.

*Unconfigured* -- no credentials anywhere, so the manager boots with no ``auth`` at
all, logs a one-time setup URL, and redirects every route except ``/setup``,
``/static`` and ``/healthz`` to the wizard. Completing the wizard builds ``auth`` in
place and closes ``/setup`` permanently; no restart is needed.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException
from starlette.requests import ClientDisconnect
from starlette.websockets import WebSocketDisconnect

from .auth import LOGIN_ERROR, AuthConfigError, SessionAuth
from .docker_control import DockerControl, DockerControlError
from .modifiers import (
    CATEGORIES as MODIFIER_CATEGORIES,
    FIELD_KEYS as MODIFIER_FIELD_KEYS,
    LABELS as MODIFIER_LABELS,
    PRESETS as MODIFIER_PRESETS,
    TOGGLES as MODIFIER_TOGGLES,
)
from .modifiers import parse as parse_modifiers
from .settings_store import (
    MASK,
    SettingsFileError,
    SettingsStore,
    format_env_value,
    is_untouched_secret,
)
from .setup import (
    MIN_PASSWORD_LENGTH,
    MIN_SERVER_PASS_LENGTH,
    SETTINGS_KEYS,
    SETUP_PATH,
    SetupInputError,
    SetupSession,
    one_line,
    validated_modifiers,
    validated_settings,
)
from .state_store import ManagerState, StateStore, StateStoreError
from .mods import (
    DEFAULT_MAX_MOD_FILES,
    DEFAULT_MAX_MOD_MB,
    DEFAULT_GAME_PLUGINS_DIR,
    DEFAULT_MODS_DIR,
    DEFAULT_STAGING_PLUGINS_DIR,
    ModError,
    ModStore,
)
from .worlds import (
    DEFAULT_BACKUPS_DIR,
    DEFAULT_MAX_UPLOAD_FILES,
    DEFAULT_MAX_UPLOAD_MB,
    DEFAULT_WORLDS_DIR,
    LEGACY_WARNING,
    WorldCollisionError,
    WorldError,
    WorldStore,
    WorldTooLargeError,
    human_size,
    opened_upload,
    oversize_message,
    plan_upload,
)
from .worlds import sanitised_name as sanitised_world_name

log = logging.getLogger("valheim_manager")

CREDENTIAL_ENV_VARS = ("ADMIN_USER", "ADMIN_PASSWORD_HASH", "SESSION_SECRET")

# Paths that must still answer while the manager is unconfigured: the wizard itself,
# its stylesheet, and the healthcheck. The healthcheck is about honesty, not
# restarts -- Docker Engine and Compose only ever act on process exit, never on
# health status. But a manager awaiting setup is running correctly, so reporting it
# unhealthy would be a lie: it would show as `(unhealthy)` in `docker ps`, hold up
# anything gated on `depends_on: condition: service_healthy`, and send the operator
# hunting a fault instead of reading the setup URL two lines up in the log.
_UNCONFIGURED_ALLOWED = (SETUP_PATH, "/healthz")

APP_DIR = Path(__file__).resolve().parent

# Confirmed against the image's own documentation: the Valheim dedicated server
# prints "Game server connected" once it is registered and accepting players.
# Override via READY_LOG_PATTERN if a game update changes the wording.
DEFAULT_READY_PATTERN = r"game server connected|Ready for connections"


# A poll faster than this is a busy loop against the socket proxy, not a feature.
MIN_LOG_POLL_SECONDS = 0.2

# Non-file form fields in an upload. Only `name` is expected; the rest is slack.
MAX_UPLOAD_FIELDS = 32
# Multipart boundaries, part headers and the trailing epilogue, none of which are the
# world: allowance so a world sitting exactly on the cap is not refused for its
# envelope. The summed part sizes are what the cap is actually enforced against.
_MULTIPART_ENVELOPE_SLACK = 8 * 1024 * 1024
# Below this a session expires before the operator can use it, and the browser just
# bounces between / and /login with nothing to explain why.
MIN_SESSION_MAX_AGE_SECONDS = 60


class ConfigError(RuntimeError):
    """A configuration value is unusable and the app must refuse to start."""


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    """Parse an int env var, falling back to ``default``, clamped to ``minimum``.

    Clamping rather than silently accepting is deliberate: ``LOG_TAIL_LINES=0``
    or a negative poll interval would break the UI in ways that look like a bug
    in the manager rather than a bad setting.
    """
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        log.warning("%s is not an integer; using %s", name, default)
        value = default
    if minimum is not None and value < minimum:
        log.warning("%s=%s is below the minimum %s; using %s", name, value, minimum, minimum)
        value = minimum
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        log.warning("%s is not a number; using %s", name, default)
        value = default
    if minimum is not None and value < minimum:
        # A zero/negative poll interval turns the pump into a busy loop that
        # hammers the socket proxy once per connected browser tab.
        log.warning("%s=%s is below the minimum %s; using %s", name, value, minimum, minimum)
        value = minimum
    return value


def _session_max_age_from_env() -> int:
    """Session lifetime, refusing rather than clamping a value that breaks login."""
    name = "SESSION_MAX_AGE_SECONDS"
    raw = os.environ.get(name, "").strip()
    if not raw:
        return 7 * 24 * 3600
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not an integer.") from exc
    if value < MIN_SESSION_MAX_AGE_SECONDS:
        raise ConfigError(
            f"{name}={value} is too small; a session that short expires before it can "
            f"be used and the browser loops between / and /login. Use at least "
            f"{MIN_SESSION_MAX_AGE_SECONDS} seconds."
        )
    return value


@dataclass
class AppConfig:
    docker_host: str = "tcp://docker-socket-proxy:2375"
    container_name: str = "valheim-server"
    image: str = "ghcr.io/community-valheim-tools/valheim-server:latest"
    network: str = "valheim-mgr-frontend"
    config_volume: str = "valheim-config"
    data_volume: str = "valheim-data"
    # Inside the mounted settings *directory*: a missing file is the manager's to
    # create, whereas a missing single-file bind mount becomes a host directory.
    env_file: str = "/srv/settings/valheim.env"
    # On the game's own `valheim-config` volume, which the manager now mounts too.
    # The game server owns this directory; the manager only ever adds worlds to it.
    worlds_dir: str = DEFAULT_WORLDS_DIR
    backups_dir: str = DEFAULT_BACKUPS_DIR
    mods_dir: str = DEFAULT_MODS_DIR
    staging_plugins_dir: str = DEFAULT_STAGING_PLUGINS_DIR
    game_plugins_dir: str = DEFAULT_GAME_PLUGINS_DIR
    mod_upload_max_bytes: int = DEFAULT_MAX_MOD_MB * 1024 * 1024
    mod_upload_max_files: int = DEFAULT_MAX_MOD_FILES
    world_upload_max_bytes: int = DEFAULT_MAX_UPLOAD_MB * 1024 * 1024
    world_upload_max_files: int = DEFAULT_MAX_UPLOAD_FILES
    # On the manager-owned `valheim-manager-state` volume, mode 0600.
    state_file: str = "/srv/state/manager-state.json"
    # Only used to print a clickable setup URL; the manager never calls itself.
    manager_url: str = ""
    restart_policy: str = "unless-stopped"
    stop_timeout: int = 120
    ready_pattern: str = DEFAULT_READY_PATTERN
    log_tail_lines: int = 200
    log_poll_seconds: float = 1.0
    status_every_n_polls: int = 2
    allowed_origins: list[str] = field(default_factory=list)
    admin_user: str = ""
    admin_password_hash: str = ""
    session_secret: str = ""
    session_max_age_seconds: int = 7 * 24 * 3600
    cookie_secure: bool = False


def config_from_env() -> AppConfig:
    origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
    return AppConfig(
        docker_host=os.environ.get("DOCKER_HOST", "tcp://docker-socket-proxy:2375"),
        container_name=os.environ.get("VALHEIM_CONTAINER_NAME", "valheim-server"),
        image=os.environ.get("VALHEIM_IMAGE", AppConfig.image),
        network=os.environ.get("VALHEIM_NETWORK", "valheim-mgr-frontend"),
        config_volume=os.environ.get("VALHEIM_CONFIG_VOLUME", "valheim-config"),
        data_volume=os.environ.get("VALHEIM_DATA_VOLUME", "valheim-data"),
        env_file=os.environ.get("VALHEIM_ENV_FILE", AppConfig.env_file),
        worlds_dir=os.environ.get("VALHEIM_WORLDS_DIR", AppConfig.worlds_dir),
        backups_dir=os.environ.get("VALHEIM_BACKUPS_DIR", AppConfig.backups_dir),
        mods_dir=os.environ.get("VALHEIM_MODS_DIR", AppConfig.mods_dir),
        staging_plugins_dir=os.environ.get(
            "VALHEIM_STAGING_PLUGINS_DIR", AppConfig.staging_plugins_dir
        ),
        game_plugins_dir=os.environ.get(
            "VALHEIM_GAME_PLUGINS_DIR", AppConfig.game_plugins_dir
        ),
        mod_upload_max_bytes=_env_int(
            "MOD_UPLOAD_MAX_MB", DEFAULT_MAX_MOD_MB, minimum=1
        )
        * 1024
        * 1024,
        mod_upload_max_files=_env_int(
            "MOD_UPLOAD_MAX_FILES", DEFAULT_MAX_MOD_FILES, minimum=1
        ),
        world_upload_max_bytes=_env_int(
            "WORLD_UPLOAD_MAX_MB", DEFAULT_MAX_UPLOAD_MB, minimum=1
        )
        * 1024
        * 1024,
        world_upload_max_files=_env_int(
            "WORLD_UPLOAD_MAX_FILES", DEFAULT_MAX_UPLOAD_FILES, minimum=2
        ),
        state_file=os.environ.get("MANAGER_STATE_FILE", AppConfig.state_file),
        manager_url=os.environ.get("MANAGER_URL", "").strip(),
        restart_policy=os.environ.get("VALHEIM_RESTART_POLICY", "unless-stopped"),
        stop_timeout=_env_int("VALHEIM_STOP_TIMEOUT", 120, minimum=1),
        ready_pattern=os.environ.get("READY_LOG_PATTERN") or DEFAULT_READY_PATTERN,
        log_tail_lines=_env_int("LOG_TAIL_LINES", 200, minimum=1),
        log_poll_seconds=_env_float("LOG_POLL_SECONDS", 1.0, minimum=MIN_LOG_POLL_SECONDS),
        status_every_n_polls=_env_int("STATUS_EVERY_N_POLLS", 2, minimum=1),
        allowed_origins=origins,
        admin_user=os.environ.get("ADMIN_USER", ""),
        admin_password_hash=os.environ.get("ADMIN_PASSWORD_HASH", ""),
        session_secret=os.environ.get("SESSION_SECRET", ""),
        session_max_age_seconds=_session_max_age_from_env(),
        cookie_secure=_env_bool("COOKIE_SECURE", False),
    )


async def _json_body(request: Request) -> dict[str, Any]:
    """Tolerant JSON body read -- an empty or malformed body is just ``{}``."""
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


class ApiError(Exception):
    def __init__(self, status_code: int, message: str, **extra: Any):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.extra = extra

    def payload(self) -> dict[str, Any]:
        return {"error": self.message, **self.extra}


def _configure_logging() -> None:
    """Make sure the manager's own log lines actually reach the container log.

    Without this the setup URL -- the one thing a clean install cannot be completed
    without -- would be swallowed: uvicorn configures only its own loggers, the root
    logger keeps no handler, and anything below WARNING from this module disappears.
    """
    if log.handlers or logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )


def _env_credentials(config: AppConfig) -> ManagerState | None:
    """Credentials from the environment, or ``None`` when none are set.

    All three or nothing. A partial set used to mean "silently ignored", which on a
    build with a setup wizard would be far worse: an operator who supplies a hash and
    a secret but forgets ``ADMIN_USER`` would be handed an admin-creation page
    instead of an error, and would have no idea why their credentials do nothing.
    """
    supplied = {
        "ADMIN_USER": config.admin_user.strip(),
        "ADMIN_PASSWORD_HASH": config.admin_password_hash.strip(),
        "SESSION_SECRET": config.session_secret.strip(),
    }
    if not any(supplied.values()):
        return None
    missing = [name for name in CREDENTIAL_ENV_VARS if not supplied[name]]
    if missing:
        raise ConfigError(
            f"{', '.join(name for name in CREDENTIAL_ENV_VARS if supplied[name])} is set "
            f"but {', '.join(missing)} is not. Configuring the manager by environment is "
            "all three variables or none of them -- set the rest, or unset all of them "
            "and use the first-run setup wizard instead."
        )
    return ManagerState(
        admin_user=supplied["ADMIN_USER"],
        admin_password_hash=config.admin_password_hash.strip(),
        session_secret=config.session_secret,
        setup_completed=True,
    )


def _build_auth(config: AppConfig, state: ManagerState) -> SessionAuth:
    """Raises ``AuthConfigError`` on a weak secret or a non-bcrypt/argon2 hash, so a
    misconfigured manager fails loudly at startup instead of at the login screen."""
    return SessionAuth(
        admin_user=state.admin_user,
        admin_password_hash=state.admin_password_hash,
        session_secret=state.session_secret,
        max_age_seconds=config.session_max_age_seconds,
        cookie_secure=config.cookie_secure,
    )


def _resolve_credentials(
    config: AppConfig, state_store: StateStore
) -> tuple[SessionAuth | None, str]:
    """``(auth, source)``; ``auth`` is ``None`` only when setup has never run.

    Environment first, then the state file. Never swallows a failure into ``None``:
    an unusable hash in either place refuses the boot, because the alternative is
    reopening the wizard on a manager that already has an admin account.
    """
    state = _env_credentials(config)
    source = "environment"
    if state is None:
        state = state_store.load()  # raises StateStoreError on a corrupt document
        source = "state file"
    if state is None:
        return None, "unconfigured"
    try:
        return _build_auth(config, state), source
    except AuthConfigError as exc:
        if source == "state file":
            raise StateStoreError(
                f"The manager state file {state_store.path} holds credentials this "
                f"manager cannot use: {exc} Restore the file from a backup, or delete "
                "it to run first-run setup again (which creates a new admin account "
                "and signs out every existing session)."
            ) from exc
        raise


def create_app(
    config: AppConfig | None = None,
    controller: DockerControl | None = None,
    settings: SettingsStore | None = None,
    state_store: StateStore | None = None,
    worlds: WorldStore | None = None,
    mods: ModStore | None = None,
) -> FastAPI:
    _configure_logging()
    config = config or config_from_env()
    store = settings or SettingsStore(config.env_file)
    states = state_store or StateStore(config.state_file)
    # The store owns both upload limits from here on: the route, the payload and the
    # panel all read them off it, so an injected store cannot enforce one number while
    # the page states another.
    world_store = worlds or WorldStore(
        config.worlds_dir,
        max_upload_bytes=config.world_upload_max_bytes,
        max_upload_files=config.world_upload_max_files,
        backups_dir=config.backups_dir,
    )
    mod_store = mods or ModStore(
        config.mods_dir,
        staging_plugins_dir=config.staging_plugins_dir,
        game_plugins_dir=config.game_plugins_dir,
        max_upload_bytes=config.mod_upload_max_bytes,
        max_upload_files=config.mod_upload_max_files,
    )

    def _loaded_world() -> str:
        """The world the next Start will open, or "" when the file cannot be read."""
        try:
            return store.read().get("WORLD_NAME", "").strip()
        except SettingsFileError:
            return ""

    def _mods_for_the_next_start() -> None:
        """Put the loaded world's mods in front of BepInEx. Runs just before create.

        Mods are per world, and BepInEx has one plugins folder -- so the set has to be
        swapped at the last moment before the container reads it. Doing it here rather
        than on Load also covers the case nobody thinks about: the operator changes
        nothing, presses Start, and expects the world's own mods.
        """
        world = _loaded_world()
        if not world:
            return
        mod_store.sync(world)

    def _container_env() -> dict[str, str]:
        """The settings file, plus the BepInEx switch the operator never has to flip.

        Automatic by design: a world with mods starts with BepInEx on, one without
        starts with it off, and nothing has to be remembered. Two things are left
        alone -- a BEPINEX the operator set themselves, and any install running
        VALHEIM_PLUS, which the image refuses to run alongside BepInEx.
        """
        environment = dict(store.container_env())
        if "BEPINEX" in environment:
            return environment
        if environment.get("VALHEIM_PLUS", "").strip().lower() in ("1", "true", "yes"):
            return environment
        world = _loaded_world()
        environment["BEPINEX"] = "true" if world and mod_store.has_enabled(world) else "false"
        return environment

    auth, credential_source = _resolve_credentials(config, states)

    # Only ever creates a file that does not exist (O_EXCL), so an operator's own
    # settings file is left byte-identical. A failure here is not fatal: the
    # credentials half of the install still works and the UI shows the named error.
    try:
        if store.ensure_default():
            log.info("Wrote a default settings file at %s.", store.path)
    except SettingsFileError as exc:
        log.warning("Could not create a default settings file: %s", exc)

    setup: SetupSession | None = None
    if auth is None:
        # Refuse now, naming the path, rather than after the operator has typed a
        # password into the wizard.
        states.ensure_writable()
        setup = SetupSession(states, store)
        _log_setup_banner(config, setup)
    else:
        log.info("Admin credentials loaded from the %s; /setup is closed.", credential_source)
    control = controller or DockerControl(
        base_url=config.docker_host,
        container_name=config.container_name,
        image=config.image,
        network=config.network,
        config_volume=config.config_volume,
        data_volume=config.data_volume,
        env_provider=_container_env,
        before_create=_mods_for_the_next_start,
        port_provider=lambda: _udp_ports(store),
        ready_pattern=config.ready_pattern,
        stop_timeout=config.stop_timeout,
        restart_policy=config.restart_policy,
        api_timeout=config.stop_timeout + 30,
    )
    # Attached rather than only passed to the constructor above, so an injected
    # controller gets them too. Staging a world's mods and deciding whether BepInEx
    # is on are the app's behaviour, not the Docker layer's -- swapping the Docker
    # plumbing is not a way to opt out of them, and a test that injected a controller
    # would otherwise exercise a manager that quietly has no mod support at all.
    control.env_provider = _container_env
    control.before_create = _mods_for_the_next_start

    app = FastAPI(title="Valheim Server Manager", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    # `auth` is None until setup completes and is then swapped in place, so every
    # guard below reads it off app.state rather than closing over the boot value.
    app.state.auth = auth
    app.state.setup = setup
    app.state.control = control
    app.state.settings = store
    app.state.state_store = states
    app.state.worlds = world_store
    app.state.mods = mod_store

    templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
    app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

    @app.exception_handler(ApiError)
    async def _api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.payload(), status_code=exc.status_code)

    # ------------------------------------------------------------- guards

    @app.middleware("http")
    async def _gate_on_setup(request: Request, call_next):
        """While unconfigured, every route but the wizard leads to the wizard.

        A single gate rather than a check per route: a page that answered normally
        here would either leak status or hand out a login form no password can pass.
        """
        if app.state.auth is None:
            path = request.url.path
            if path not in _UNCONFIGURED_ALLOWED and not path.startswith("/static/"):
                return RedirectResponse(SETUP_PATH, status_code=302)
        return await call_next(request)

    def session_of(request: Request):
        current = app.state.auth
        if current is None:
            return None
        return current.read_token(request.cookies.get(current.cookie_name))

    def require_session(request: Request):
        session = session_of(request)
        if session is None:
            raise ApiError(401, "Authentication required.")
        return session

    def require_same_origin(request: Request) -> None:
        """Reject state-changing requests whose Origin is foreign or absent."""
        origin = request.headers.get("origin")
        if not origin:
            raise ApiError(403, "Request rejected: missing Origin header.")
        allowed = set(config.allowed_origins)
        host = request.headers.get("host")
        if host:
            allowed |= {f"http://{host}", f"https://{host}"}
        if origin.rstrip("/") not in {a.rstrip("/") for a in allowed}:
            raise ApiError(403, "Request rejected: cross-site origin.")

    def display_settings() -> tuple[list[dict[str, Any]], str | None]:
        try:
            return store.display_settings(), None
        except SettingsFileError as exc:
            return [], str(exc)

    def modifier_state() -> dict[str, Any]:
        """The world modifiers the file encodes, as the panel's controls see them.

        Parsed server-side and pushed with every status, so the browser never has to
        own the vocabulary or the ordering rule -- it fills its controls from this and
        composes the preview from what the page was rendered with. An unreadable file
        is already reported through ``settings_error``; here it simply means no
        modifiers, so the panel does not also break.
        """
        try:
            raw = store.read().get("SERVER_ARGS", "")
        except SettingsFileError:
            raw = ""
        return parse_modifiers(raw).as_dict()

    def server_clock() -> dict[str, Any]:
        """The wall clock of the machine the manager runs on.

        The dashboard's clock is deliberately the *server's* time, not the browser's:
        a log line, a backup stamp and a "started at" all come from this clock, so a
        header showing the operator's own laptop time would disagree with everything
        else on the page.

        Sent as an anchor -- epoch plus the offset that was in force at that instant
        -- rather than as a formatted string. The browser ticks locally between
        pushes, which keeps the clock off the network, and re-anchors on every push,
        which keeps it from drifting. The offset travels with it so the page can show
        the server's wall clock without knowing anything about time zones.

        The zone comes from the container's own TZ. Compose passes TZ through to the
        manager service; unset, a container is UTC, which is why the label is always
        drawn next to the time instead of being left implicit.
        """
        now = datetime.now().astimezone()
        offset = now.utcoffset()
        seconds = int(offset.total_seconds()) if offset else 0
        # tzname() is a short abbreviation on glibc, which is what the container runs
        # ("IDT"), but a full sentence on Windows, where the tests and a dev run
        # happen ("Jerusalem Daylight Time"). The clock has room for the first and
        # not the second, so anything that long becomes its offset instead -- which
        # is never ambiguous, even if it is less friendly.
        name = now.tzname() or ""
        if not name or len(name) > 6:
            magnitude = abs(seconds)
            name = "UTC%s%d" % ("+" if seconds >= 0 else "-", magnitude // 3600)
            if magnitude % 3600:
                name += ":%02d" % (magnitude % 3600 // 60)
        return {
            "epoch": now.timestamp(),
            "offset_minutes": seconds // 60,
            "zone": name,
        }

    def status_envelope() -> dict[str, Any]:
        """The full status payload, as both /api/status and the log socket send it.

        One builder rather than two literals: the socket and the REST route have to
        agree, and they did not disagree only because nothing had been added to the
        shape since they were written.
        """
        rows, settings_error = display_settings()
        return {
            "status": safe_status(),
            "settings": rows,
            "settings_error": settings_error,
            "modifiers": modifier_state(),
            "server_clock": server_clock(),
        }

    def safe_status() -> dict[str, Any]:
        try:
            return control.status()
        except DockerControlError as exc:
            return {
                "phase": "error",
                "container_exists": False,
                "container_state": None,
                "ready": False,
                "message": exc.message,
                "error": exc.message,
                "docker_error": exc.docker_message,
                "image": config.image,
                "container_name": config.container_name,
                "started_at": None,
                "exit_code": None,
                "ready_pattern": config.ready_pattern,
            }

    # -------------------------------------------------------------- pages

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> Response:
        if session_of(request) is None:
            return RedirectResponse("/login", status_code=302)
        rows, settings_error = display_settings()
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "settings_rows": rows,
                "settings_error": settings_error,
                "container_name": config.container_name,
                "image": config.image,
                # The editor spells both of these out to the operator and posts the
                # first one back untouched, so they come from their definitions rather
                # than from a copy in the markup that nothing keeps honest.
                "mask": MASK,
                "min_server_pass_length": MIN_SERVER_PASS_LENGTH,
                "admin_user": app.state.auth.admin_user,
                # The modifier vocabulary is rendered from its definitions, and the
                # category controls are rendered in the composer's own order -- the
                # preview walks them in document order, so the page cannot disagree
                # with the string the manager will write.
                "modifier_presets": MODIFIER_PRESETS,
                "modifier_categories": MODIFIER_CATEGORIES,
                "modifier_toggles": MODIFIER_TOGGLES,
                "modifier_labels": MODIFIER_LABELS,
                # The worlds panel states its own caps and says where the worlds live,
                # all from the store that will actually enforce them.
                # Rendered into the markup as well as pushed, because the browser
                # gets status only from the log socket: without this the clock would
                # sit blank from page load until the first push connects.
                "server_clock": server_clock(),
                "worlds_dir": str(world_store.root),
                "mod_upload_max_human": human_size(mod_store.max_upload_bytes),
                "max_upload_bytes": world_store.max_upload_bytes,
                "max_upload_human": human_size(world_store.max_upload_bytes),
                "max_upload_files": world_store.max_upload_files,
                "legacy_warning": LEGACY_WARNING,
            },
        )

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request) -> Response:
        if session_of(request) is not None:
            return RedirectResponse("/", status_code=302)
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login")
    async def login_submit(
        request: Request,
        username: str = Form(default=""),
        password: str = Form(default=""),
    ) -> Response:
        require_same_origin(request)
        current = app.state.auth
        # Off the event loop: bcrypt at cost 12 and argon2 each hold the GIL for
        # hundreds of milliseconds, which would stall every other request -- including
        # the _pump loop of every open log console -- on each login attempt.
        if not await run_in_threadpool(current.check_credentials, username, password):
            return templates.TemplateResponse(
                request, "login.html", {"error": LOGIN_ERROR}, status_code=401
            )
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            value=current.issue_token(current.admin_user), **current.cookie_kwargs()
        )
        return response

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        require_same_origin(request)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(app.state.auth.cookie_name, path="/")
        return response

    # ------------------------------------------------------- first-run setup

    def setup_page(
        request: Request,
        *,
        refused: bool = False,
        token: str = "",
        error: str | None = None,
        values: dict[str, str] | None = None,
        status_code: int = 200,
    ) -> Response:
        return templates.TemplateResponse(
            request,
            "setup.html",
            {
                "refused": refused,
                "token": token,
                "error": error,
                "values": values or {},
                "min_password_length": MIN_PASSWORD_LENGTH,
            },
            status_code=status_code,
        )

    def setup_gate(request: Request, token: str) -> Response | None:
        """``None`` when the wizard may proceed, otherwise the response to return.

        A configured manager sends the operator to /login -- even holding the original
        token -- and nothing is overwritten. A wrong, missing or reused token gets one
        refusal that says nothing about whether a token exists, and leaves the real
        one usable: there is no attempt counter to burn.
        """
        session: SetupSession | None = app.state.setup
        if app.state.auth is not None or session is None or not session.is_open():
            return RedirectResponse("/login", status_code=302)
        if not session.token_ok(token):
            return setup_page(request, refused=True, status_code=403)
        return None

    @app.get(SETUP_PATH, response_class=HTMLResponse)
    async def setup_form(request: Request, token: str = "") -> Response:
        blocked = setup_gate(request, token)
        if blocked is not None:
            return blocked
        return setup_page(request, token=token)

    @app.post(SETUP_PATH)
    async def setup_submit(
        request: Request,
        token: str = Form(default=""),
        admin_user: str = Form(default=""),
        password: str = Form(default=""),
        password_confirm: str = Form(default=""),
        hash_algo: str = Form(default="bcrypt"),
        server_name: str = Form(default=""),
        world_name: str = Form(default=""),
        server_port: str = Form(default=""),
        server_pass: str = Form(default=""),
        server_public: str = Form(default=""),
        crossplay: str = Form(default=""),
    ) -> Response:
        require_same_origin(request)
        blocked = setup_gate(request, token)
        if blocked is not None:
            return blocked
        session: SetupSession = app.state.setup
        # Echoed back on a validation failure so the operator retypes only what
        # actually failed. The two admin passwords are deliberately not among them.
        # The join password is: it is a visible field, and silently blanking it when
        # some unrelated field failed would write an empty one on the retry.
        entered = {
            "admin_user": admin_user,
            "hash_algo": hash_algo,
            "server_name": server_name,
            "world_name": world_name,
            "server_port": server_port,
            "server_pass": server_pass,
            "server_public": server_public,
            "crossplay": crossplay,
        }
        try:
            # Hashing, so off the event loop for the same reason as /login.
            prepared = await run_in_threadpool(
                session.prepare,
                admin_user=admin_user,
                password=password,
                confirm=password_confirm,
                algo=hash_algo,
                settings={
                    "SERVER_NAME": server_name,
                    "WORLD_NAME": world_name,
                    "SERVER_PORT": server_port,
                    "SERVER_PASS": server_pass,
                    "SERVER_PUBLIC": "1" if _checkbox(server_public) else "0",
                    "CROSSPLAY": "true" if _checkbox(crossplay) else "false",
                },
            )
            # Built before anything is written: a manager that saved credentials it
            # then could not load would be locked out with the wizard already closed.
            new_auth = _build_auth(config, prepared.state)
            session.commit(prepared)
        except (SetupInputError, AuthConfigError) as exc:
            return setup_page(
                request, token=token, error=str(exc), values=entered, status_code=400
            )
        except (SettingsFileError, StateStoreError) as exc:
            # Nothing was completed, so the same URL still works once the mount is
            # fixed. The message names the path and the cause.
            log.error("First-run setup could not write its files: %s", exc)
            return setup_page(
                request, token=token, error=str(exc), values=entered, status_code=500
            )

        app.state.auth = new_auth
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            value=new_auth.issue_token(new_auth.admin_user), **new_auth.cookie_kwargs()
        )
        return response

    # ---------------------------------------------------------------- api

    @app.get("/api/status")
    async def api_status(request: Request) -> JSONResponse:
        require_session(request)
        return JSONResponse(await run_in_threadpool(status_envelope))

    @app.post("/api/start")
    async def api_start(request: Request) -> JSONResponse:
        require_session(request)
        require_same_origin(request)
        _refuse_doomed_start(store)
        return await _run_action(control.start)

    @app.post("/api/stop")
    async def api_stop(request: Request) -> JSONResponse:
        require_session(request)
        require_same_origin(request)
        # Only a real boolean true. `{"force": "false"}` is truthy in Python, and
        # reading it as force would SIGKILL a running server -- losing the world save
        # since the last autosave -- when the caller plainly asked for the opposite.
        force = (await _json_body(request)).get("force") is True
        return await _run_action(lambda: control.stop(force=force))

    @app.post("/api/restart")
    async def api_restart(request: Request) -> JSONResponse:
        require_session(request)
        require_same_origin(request)
        _refuse_doomed_start(store)
        return await _run_action(control.restart)

    # ------------------------------------------------------- settings panel

    def _settings_payload(**extra: Any) -> dict[str, Any]:
        """The panel's own refresh: status plus the masked settings, after the fact."""
        rows, settings_error = display_settings()
        return {
            "status": safe_status(),
            "settings": rows,
            "settings_error": settings_error,
            "modifiers": modifier_state(),
            **extra,
        }

    def _refused(status_code: int, error: str, **extra: Any) -> JSONResponse:
        """Every refusal answers the same shape: why, plus a fresh view of the file and
        the server. Without the refresh a save that is both invalid *and* racing a start
        would leave the editor open over a panel that is now locked."""
        payload: dict[str, Any] = {"error": error, **extra}
        payload.update(_settings_payload(saved=False))
        return JSONResponse(payload, status_code=status_code)

    def _save_settings(body: dict[str, Any]) -> JSONResponse:
        """Validate, write, and remove the stopped container. Blocking, so it runs in a
        threadpool -- every step here is file or Docker I/O.

        The order is the contract: prove the server is off, then validate, then write,
        then remove. Nothing is written until every check has passed, so a refusal
        leaves the file exactly as it was.
        """
        try:
            submitted = _submitted_settings(body)
            modifier_fields = _submitted_modifiers(body)
        except SetupInputError as exc:
            return _refused(400, str(exc))

        try:
            reason = control.running_reason()
        except DockerControlError as exc:
            # The server cannot be *shown* to be off, and writing anyway is the one
            # thing this route must never do.
            return _refused(
                502,
                f"{exc.message} The settings were not saved: the manager could not "
                "confirm the server is stopped.",
                docker_error=exc.docker_message,
            )
        if reason:
            # Re-checked here and not only in the browser: the panel's view of the phase
            # is up to a poll interval old, and the server may have been started from
            # another tab or from the host since the editor was opened.
            return _refused(409, reason)

        try:
            current = store.read()
        except SettingsFileError as exc:
            # A file the manager cannot read is a fault on this side, not a malformed
            # request -- the same status the write failure below answers with.
            return _refused(500, str(exc))

        if modifier_fields is not None:
            # Composed here, not in the browser: the ordering rule is correctness (a
            # preset emitted after a modifier flattens it), and whatever the file's
            # SERVER_ARGS holds outside this vocabulary has to be carried through. Both
            # need the file, so this sits after the read and before the diff -- from
            # here on SERVER_ARGS is just another key on the existing path.
            try:
                submitted["SERVER_ARGS"] = validated_modifiers(
                    modifier_fields, current=current.get("SERVER_ARGS", "")
                )
            except SetupInputError as exc:
                return _refused(400, str(exc))

        # The mask is not a value. A secret posted back untouched is dropped right here,
        # before validation and long before the writer, so `********` can never become
        # the join password.
        proposed = {
            key: one_line(value).strip()
            for key, value in submitted.items()
            if not is_untouched_secret(key, value)
        }
        # Only what actually differs is validated: a value already in the file that this
        # submission does not touch is not this save's problem (an empty SERVER_PASS,
        # say, must not block a rename -- Start refuses that one on its own).
        touched = {key: value for key, value in proposed.items() if current.get(key, "") != value}
        try:
            values = validated_settings(touched, current=current)
        except SetupInputError as exc:
            return _refused(400, str(exc))

        # Validation normalises (a stripped port, for instance), so the diff is redone
        # against what would actually be written.
        updates = {key: value for key, value in values.items() if current.get(key, "") != value}
        if not updates:
            return JSONResponse(
                _settings_payload(
                    saved=True,
                    changed=[],
                    container_removed=False,
                    message="Nothing to save -- nothing changed.",
                )
            )
        for key, value in updates.items():
            # A value an env file cannot represent is bad input, not a failed write, and
            # it has to be refused before the file is touched rather than halfway
            # through rendering it.
            try:
                format_env_value(key, value)
            except SettingsFileError as exc:
                return _refused(400, str(exc))

        try:
            store.write(updates)
        except SettingsFileError as exc:
            return _refused(500, str(exc))
        # Keys only, never values: the join password is one of them.
        log.info("Settings panel updated %s in %s.", ", ".join(sorted(updates)), store.path)

        return _recreate_after_write(sorted(updates), _settings_payload)

    def _recreate_after_write(
        changed: list[str], payload_for: Any, *, saved_line: str | None = None
    ) -> JSONResponse:
        """Remove the stopped container, then answer with the panel's own refresh.

        The last step of every write to the settings file, shared by the settings panel
        and the world switch, because it is the step that makes a saved value real:
        ``start()`` creates a container only when none exists, so a file written with
        the old container still around is a change the next Start silently ignores.
        """
        saved = saved_line or f"Saved {', '.join(changed)}."
        try:
            removed = control.remove_stopped_container()
        except DockerControlError as exc:
            # The file is written, so say so plainly and name what is still owed: Start
            # would otherwise reuse the container and its old environment.
            payload = exc.as_dict()
            payload["error"] = (
                f"{exc.message} Your settings were saved, but the old server is still "
                "there and Start would bring it back with the old settings. Remove it "
                f"with `docker rm {config.container_name}` and press Start."
            )
            payload.update(
                payload_for(saved=True, changed=changed, container_removed=False)
            )
            return JSONResponse(payload, status_code=502)

        message = (
            f"{saved} Press Start and your server comes back with the new settings. "
            "Your world and your backups were not touched."
            if removed
            else f"{saved} Press Start whenever you are ready."
        )
        return JSONResponse(
            payload_for(
                saved=True, changed=changed, container_removed=removed, message=message
            )
        )

    @app.post("/api/settings")
    async def api_settings(request: Request) -> JSONResponse:
        require_session(request)
        require_same_origin(request)
        # The body is parsed inside _save_settings, in the threadpool, so that a
        # rejected field answers with the same refreshed payload as every other refusal.
        return await run_in_threadpool(_save_settings, await _json_body(request))

    # ---------------------------------------------------------- worlds panel

    def _worlds_payload(**extra: Any) -> dict[str, Any]:
        """The worlds on the volume, the active one marked, plus the upload cap.

        An unreadable volume is reported as ``worlds_error`` rather than raised: the
        panel has to render, and a mount the operator can fix is not a reason to take
        the rest of the dashboard down with it.
        """
        try:
            active = store.read().get("WORLD_NAME", "")
        except SettingsFileError:
            # Already surfaced as `settings_error` by the panel refresh; here it only
            # means the manager cannot say which world is active.
            active = ""
        worlds_error: str | None = None
        rows: list[dict[str, Any]] = []
        try:
            rows = [
                world.as_dict(active=world.name == active) for world in world_store.worlds()
            ]
        except WorldError as exc:
            worlds_error = str(exc)
        return {
            "worlds": rows,
            "active_world": active,
            "worlds_error": worlds_error,
            "worlds_dir": str(world_store.root),
            "max_upload_bytes": world_store.max_upload_bytes,
            # Formatted here rather than in the browser: two size formatters would be
            # two chances to state a different limit from the one enforced.
            "max_upload": human_size(world_store.max_upload_bytes),
            "max_upload_files": world_store.max_upload_files,
            **extra,
        }

    def _world_panel(**extra: Any) -> dict[str, Any]:
        """One refresh shape for every world answer: the settings panel's, plus worlds.

        A switch writes ``WORLD_NAME`` and removes the container, so the settings table
        and the status badge both move underneath the operator -- sending the whole
        panel back is what keeps the two halves of the page from disagreeing.
        """
        payload = _settings_payload()
        payload.update(_worlds_payload())
        payload.update(extra)
        return payload

    def _world_refused(status_code: int, error: str, **extra: Any) -> JSONResponse:
        payload: dict[str, Any] = {"error": error, **extra}
        payload.update(_world_panel(saved=False))
        return JSONResponse(payload, status_code=status_code)

    def _upload_refused(status_code: int, error: str) -> JSONResponse:
        """A refused upload, logged as well as answered.

        Without this the whole upload path is silent on the host: a rejected traversal
        entry, a symlink in an archive, or a run of oversize attempts would produce one
        JSON error in one browser and no trace anywhere else -- the one part of this
        panel's threat model with no audit trail.
        """
        log.warning("World upload refused (%s): %s", status_code, error)
        return _world_refused(status_code, error)

    def _world_action_blocked() -> JSONResponse | None:
        """The gate both world actions share, or ``None`` when the server is off.

        Re-checked server-side on every call and, for an upload, again just before the
        volume is touched: the browser's view of the phase is up to a poll interval old
        and a few hundred megabytes take longer than that to arrive.
        """
        try:
            reason = control.running_reason()
        except DockerControlError as exc:
            return _world_refused(
                502,
                f"{exc.message} Nothing was changed -- there was no way to check "
                "that your server is off.",
                docker_error=exc.docker_message,
            )
        if reason:
            return _world_refused(409, reason)
        return None

    @app.get("/api/worlds")
    async def api_worlds(request: Request) -> JSONResponse:
        require_session(request)
        # Not pushed with the status: listing walks the volume, and doing that every
        # couple of seconds per open tab is a cost the panel does not need to pay.
        return JSONResponse(await run_in_threadpool(_worlds_payload))

    def _switch_world(body: dict[str, Any]) -> JSONResponse:
        """Point ``WORLD_NAME`` at an existing world, through the settings path.

        Same order as a settings save, for the same reasons: prove the server is off,
        validate, write, remove the stopped container. Nothing is written until every
        check has passed.
        """
        raw = body.get("name")
        if not isinstance(raw, str):
            return _world_refused(400, "Say which world to load.")
        try:
            name = sanitised_world_name(raw)
        except WorldError as exc:
            return _world_refused(400, str(exc))

        blocked = _world_action_blocked()
        if blocked is not None:
            return blocked

        # Only a world that is actually on the volume. Pointing WORLD_NAME at a name
        # that does not exist is how the next Start generates a *brand new* world with
        # a new seed, which is never what picking one off this list meant.
        try:
            known = world_store.worlds()
        except WorldError as exc:
            return _world_refused(500, str(exc))
        if not any(world.name == name for world in known):
            return _world_refused(
                404,
                f"There is no world called {name!r} on the server. Nothing was "
                "changed -- refresh the list, or upload that world first.",
            )

        try:
            current = store.read()
        except SettingsFileError as exc:
            return _world_refused(500, str(exc))
        try:
            # The same validator the wizard and the settings panel use, so a world name
            # the panel would refuse cannot get in by this door instead.
            values = validated_settings({"WORLD_NAME": name}, current=current)
        except SetupInputError as exc:
            return _world_refused(400, str(exc))

        if current.get("WORLD_NAME", "") == values["WORLD_NAME"]:
            return JSONResponse(
                _world_panel(
                    saved=True,
                    changed=[],
                    container_removed=False,
                    message=f"{name} is already the world your server loads.",
                )
            )
        try:
            format_env_value("WORLD_NAME", values["WORLD_NAME"])
        except SettingsFileError as exc:
            return _world_refused(400, str(exc))
        try:
            store.write(values)
        except SettingsFileError as exc:
            return _world_refused(500, str(exc))
        log.info("Worlds panel set WORLD_NAME to %r in %s.", name, store.path)
        return _recreate_after_write(
            ["WORLD_NAME"], _world_panel, saved_line=f"Your server will load {name} next time you start it."
        )

    def _delete_world(body: dict[str, Any]) -> JSONResponse:
        """Remove a world from the volume. Blocking, so: threadpool.

        Same order as every other world action -- prove the server is off, validate,
        then act -- with one extra gate that only this route needs: the world the
        server is *set to load* is refused outright. Deleting it would leave
        ``WORLD_NAME`` pointing at nothing, and the next Start would quietly generate
        a brand-new world under that name rather than reporting a problem. Switching
        first, or creating a new world first, is the safe order and the refusal says so.
        """
        raw = body.get("name")
        if not isinstance(raw, str):
            return _world_refused(400, "Say which world to delete.")
        try:
            name = sanitised_world_name(raw)
        except WorldError as exc:
            return _world_refused(400, str(exc))

        blocked = _world_action_blocked()
        if blocked is not None:
            return blocked

        try:
            active = store.read().get("WORLD_NAME", "")
        except SettingsFileError as exc:
            return _world_refused(500, str(exc))
        if active and active.strip().lower() == name.lower():
            return _world_refused(
                409,
                f"{name} is the world your server is set to load, so it cannot be "
                "deleted. Load a different world first -- or make a new one -- and "
                "then delete this one. Nothing was deleted.",
            )

        try:
            removed = world_store.delete(name)
        except WorldError as exc:
            log.warning("World delete refused: %s", exc)
            return _world_refused(400, str(exc))
        log.info("Worlds panel deleted %r (%s).", name, ", ".join(removed))
        return JSONResponse(
            _world_panel(
                deleted=True,
                name=name,
                message=f"{name} is gone. It cannot be recovered from here.",
            )
        )

    def _backup_world(body: dict[str, Any]) -> JSONResponse:
        """Zip a world into the backups folder. Blocking, so: threadpool.

        The one world action with no server-off gate. It reads the world and writes
        somewhere else entirely, so there is nothing for a running server to collide
        with -- and a backup you can only take by stopping the server is one nobody
        takes before the risky thing they wanted it for.
        """
        raw = body.get("name")
        if not isinstance(raw, str):
            return _world_refused(400, "Say which world to back up.")
        try:
            name = sanitised_world_name(raw)
        except WorldError as exc:
            return _world_refused(400, str(exc))

        try:
            backup = world_store.backup(name)
        except WorldError as exc:
            log.warning("World backup refused: %s", exc)
            return _world_refused(400, str(exc))
        log.info("Worlds panel backed up %r as %s.", name, backup.name)
        return JSONResponse(
            _world_panel(
                backed_up=True,
                name=backup.name,
                message=(
                    f"Backed up {backup.world} as {backup.name} ({backup.size}). "
                    "It is kept until you remove it."
                ),
            )
        )

    def _mods_payload(world: str, **extra: Any) -> dict[str, Any]:
        """The mods for one world, plus which world that is and the caps."""
        payload: dict[str, Any] = {
            "world": world,
            "mods": [],
            "mods_error": None,
            "mod_upload_max_bytes": mod_store.max_upload_bytes,
            "mod_upload_max_human": human_size(mod_store.max_upload_bytes),
            "mod_upload_max_files": mod_store.max_upload_files,
        }
        try:
            payload["mods"] = [mod.as_dict() for mod in mod_store.mods(world)] if world else []
        except (ModError, WorldError) as exc:
            payload["mods_error"] = str(exc)
        payload.update(extra)
        return payload

    def _mod_world(body: dict[str, Any]) -> str:
        raw = body.get("world")
        if not isinstance(raw, str) or not raw.strip():
            raise ModError("Say which world these mods belong to.")
        return sanitised_world_name(raw)

    @app.get("/api/mods")
    async def api_mods(request: Request, world: str = "") -> JSONResponse:
        require_session(request)
        # Defaults to the world the next Start would open, which is the one the
        # operator means nine times out of ten.
        try:
            chosen = sanitised_world_name(world) if world.strip() else _loaded_world()
        except WorldError as exc:
            return JSONResponse({"error": str(exc), **_mods_payload("")}, status_code=400)
        return JSONResponse(await run_in_threadpool(_mods_payload, chosen))

    def _mod_refused(status_code: int, error: str, world: str = "") -> JSONResponse:
        log.warning("Mod action refused (%s): %s", status_code, error)
        payload: dict[str, Any] = {"error": error}
        payload.update(_mods_payload(world))
        return JSONResponse(payload, status_code=status_code)

    def _delete_mod(body: dict[str, Any]) -> JSONResponse:
        try:
            world = _mod_world(body)
            name = body.get("name")
            if not isinstance(name, str):
                raise ModError("Say which mod to delete.")
            gone = mod_store.delete(world, name)
        except (ModError, WorldError) as exc:
            return _mod_refused(400, str(exc), body.get("world", "") if isinstance(body.get("world"), str) else "")
        return JSONResponse(
            _mods_payload(world, deleted=True, message=f"{gone} is gone from {world}.")
        )

    @app.post("/api/mods/delete")
    async def api_mod_delete(request: Request) -> JSONResponse:
        require_session(request)
        require_same_origin(request)
        return await run_in_threadpool(_delete_mod, await _json_body(request))

    def _toggle_mod(body: dict[str, Any]) -> JSONResponse:
        try:
            world = _mod_world(body)
            name = body.get("name")
            if not isinstance(name, str):
                raise ModError("Say which mod to switch.")
            mod = mod_store.set_enabled(world, name, body.get("enabled") is True)
        except (ModError, WorldError) as exc:
            return _mod_refused(400, str(exc), body.get("world", "") if isinstance(body.get("world"), str) else "")
        state = "on" if mod.enabled else "off"
        return JSONResponse(
            _mods_payload(
                world,
                toggled=True,
                message=(
                    f"{mod.name} is switched {state}. It takes effect the next time "
                    "you start the server."
                ),
            )
        )

    @app.post("/api/mods/toggle")
    async def api_mod_toggle(request: Request) -> JSONResponse:
        require_session(request)
        require_same_origin(request)
        return await run_in_threadpool(_toggle_mod, await _json_body(request))

    def _place_mod(parts: list[tuple[str, Any, int]], world: str, name: str) -> JSONResponse:
        try:
            safe_world = sanitised_world_name(world)
        except WorldError as exc:
            return _mod_refused(400, str(exc))
        try:
            with opened_upload(parts) as (members, suggested):
                mod = mod_store.place(safe_world, members, name or suggested)
        except (ModError, WorldError) as exc:
            return _mod_refused(400, str(exc), safe_world)
        return JSONResponse(
            _mods_payload(
                safe_world,
                uploaded=True,
                name=mod.name,
                message=(
                    f"Added {mod.name} to {safe_world}. It loads the next time you "
                    "start the server."
                ),
            )
        )

    @app.post("/api/mods/upload")
    async def api_mod_upload(request: Request) -> JSONResponse:
        require_session(request)
        require_same_origin(request)
        try:
            async with request.form(
                max_files=mod_store.max_upload_files, max_fields=MAX_UPLOAD_FIELDS
            ) as form:
                world = form.get("world")
                name = form.get("name")
                parts: list[tuple[str, Any, int]] = []
                for _field, value in form.multi_items():
                    if isinstance(value, UploadFile):
                        parts.append((value.filename or "", value.file, _part_size(value)))
                return await run_in_threadpool(
                    _place_mod,
                    parts,
                    world if isinstance(world, str) else "",
                    name if isinstance(name, str) else "",
                )
        except (MultiPartException, HTTPException) as exc:
            detail = getattr(exc, "message", None) or getattr(exc, "detail", "") or str(exc)
            return await run_in_threadpool(
                _mod_refused, 400, f"That upload could not be read: {detail} Nothing was saved."
            )
        except ClientDisconnect:
            return await run_in_threadpool(
                _mod_refused, 400, "The upload stopped part-way. Nothing was saved."
            )

    @app.post("/api/worlds/backup")
    async def api_world_backup(request: Request) -> JSONResponse:
        require_session(request)
        require_same_origin(request)
        return await run_in_threadpool(_backup_world, await _json_body(request))

    @app.post("/api/worlds/delete")
    async def api_world_delete(request: Request) -> JSONResponse:
        require_session(request)
        require_same_origin(request)
        return await run_in_threadpool(_delete_world, await _json_body(request))

    def _new_world(body: dict[str, Any]) -> JSONResponse:
        """Point ``WORLD_NAME`` at a name nothing is using yet. Blocking: threadpool.

        The manager cannot *generate* a world -- Valheim does that itself, on the next
        Start, from whatever ``WORLD_NAME`` says. So "new world" is exactly the switch
        path with the collision check inverted: the switch insists the name already
        exists, and this insists it does not. Anything else would quietly adopt an
        existing save under the banner of making a new one.
        """
        raw = body.get("name")
        if not isinstance(raw, str):
            return _world_refused(400, "Give the new world a name.")
        try:
            name = sanitised_world_name(raw)
        except WorldError as exc:
            return _world_refused(400, str(exc))

        blocked = _world_action_blocked()
        if blocked is not None:
            return blocked

        try:
            current = store.read()
            values = validated_settings({"WORLD_NAME": name}, current=current)
        except SettingsFileError as exc:
            return _world_refused(500, str(exc))
        except SetupInputError as exc:
            return _world_refused(400, str(exc))

        # Read the directory itself, not the world list: a half world is invisible in
        # the list and would still be adopted by a Start under this name.
        try:
            blocking = world_store.blocking_entries(values["WORLD_NAME"])
        except WorldError as exc:
            return _world_refused(500, str(exc))
        if blocking:
            return _world_refused(
                409,
                f"There is already a world called {name} on the server "
                f"({', '.join(blocking)}), so this would open that one rather than "
                "make a new one. Pick a different name, or press Load on it instead.",
            )

        try:
            format_env_value("WORLD_NAME", values["WORLD_NAME"])
        except SettingsFileError as exc:
            return _world_refused(400, str(exc))
        try:
            store.write(values)
        except SettingsFileError as exc:
            return _world_refused(500, str(exc))
        log.info("Worlds panel set WORLD_NAME to a new world %r in %s.", name, store.path)
        return _recreate_after_write(
            ["WORLD_NAME"],
            _world_panel,
            saved_line=(
                f"Ready to make {name}. Press Start and Valheim builds it, with a "
                "random seed it picks at that moment."
            ),
        )

    @app.post("/api/worlds/new")
    async def api_world_new(request: Request) -> JSONResponse:
        require_session(request)
        require_same_origin(request)
        return await run_in_threadpool(_new_world, await _json_body(request))

    @app.post("/api/worlds/switch")
    async def api_world_switch(request: Request) -> JSONResponse:
        require_session(request)
        require_same_origin(request)
        return await run_in_threadpool(_switch_world, await _json_body(request))

    def _place_upload(
        parts: list[tuple[str, Any, int]], requested_name: str
    ) -> JSONResponse:
        """Validate an upload completely, then place it. Blocking, so: threadpool.

        Every refusal here happens before anything is written -- ``place`` is the only
        statement that touches the volume, and it stages and renames rather than
        writing a world's name into existence a file at a time.
        """
        cap = world_store.max_upload_bytes
        if not parts:
            return _upload_refused(
                400,
                "Nothing arrived. Drop the world's folder, a .zip of it, or a "
                ".db and .fwl pair.",
            )
        # The parser has spooled the body to its own temp files by now, so this is the
        # authoritative size check; the Content-Length one above only spares us reading
        # a body that was hopeless from the start.
        total = sum(size for _, _, size in parts)
        if total > cap:
            return _upload_refused(413, oversize_message(total, cap))

        try:
            name = sanitised_world_name(requested_name) if requested_name.strip() else ""
        except WorldError as exc:
            return _upload_refused(400, str(exc))

        try:
            with opened_upload(parts) as (members, suggested):
                declared = sum(member.size for member in members)
                if declared > cap:
                    # An archive's own directory claims these sizes; `place` counts the
                    # bytes that actually arrive as well.
                    raise WorldTooLargeError(oversize_message(declared, cap))
                plan = plan_upload(
                    members, requested_name=name, suggested_name=suggested
                )
                world_store.refuse_collision(plan.name)
                # The last gate before the volume is touched: reading the body took
                # real time, and a Start in another tab must not be overtaken here.
                blocked = _world_action_blocked()
                if blocked is not None:
                    log.warning("World upload refused: the server is not stopped.")
                    return blocked
                world = world_store.place(plan)
        # One condition, one status, whichever of the two checks caught it: the same
        # clash answered 409 from the pre-check and 400 from inside `place` before
        # these types existed, purely on timing.
        except WorldCollisionError as exc:
            return _upload_refused(409, str(exc))
        except WorldTooLargeError as exc:
            return _upload_refused(413, str(exc))
        except WorldError as exc:
            return _upload_refused(400, str(exc))

        message = (
            f"Uploaded {world.name} ({human_size(world.size_bytes)}). Press Load to "
            "play it next time you start the server."
        )
        return JSONResponse(
            _world_panel(
                uploaded=True,
                name=world.name,
                layout=world.layout,
                warning=LEGACY_WARNING if world.legacy else None,
                message=message,
            )
        )

    async def _refuse_before_body(
        request: Request, status_code: int, error: str
    ) -> JSONResponse:
        """Refuse a request whose body has not been read, having first drained it.

        Answering while the client is still sending makes most ASGI servers reset the
        connection, and the browser then reports a transport failure instead of the
        refusal this route took care to word. Draining discards the body chunk by
        chunk and never puts a byte of it on the volume, so "refused before the volume
        is touched" is still exactly what happens.
        """
        try:
            async for _chunk in request.stream():
                pass
        except ClientDisconnect:  # pragma: no cover - the client gave up first
            pass
        return await run_in_threadpool(_upload_refused, status_code, error)

    @app.post("/api/worlds/upload")
    async def api_world_upload(request: Request) -> JSONResponse:
        require_session(request)
        require_same_origin(request)
        cap = world_store.max_upload_bytes
        # Before a byte is read: a body that cannot possibly fit is refused outright, so
        # an oversized drop never reaches any disk -- not even the temp file the
        # multipart parser would otherwise spool it to. That promise only holds if the
        # size is actually declared, so an undeclared one is refused rather than quietly
        # skipping the gate. The slack is the multipart envelope, which only ever makes
        # the body bigger than the world inside it.
        declared = request.headers.get("content-length", "")
        if not declared.isdigit():
            return await _refuse_before_body(
                request,
                411,
                "That upload did not say how big it was, so it was turned away "
                "before anything was read. Nothing was saved.",
            )
        if int(declared) > cap + _MULTIPART_ENVELOPE_SLACK:
            return await _refuse_before_body(
                request, 413, oversize_message(int(declared), cap)
            )
        # Blocking Docker and file I/O, so off the event loop: a hung socket proxy here
        # would otherwise stall every open tab's log stream, not just this upload.
        blocked = await run_in_threadpool(_world_action_blocked)
        if blocked is not None:
            log.warning("World upload refused: the server is not stopped.")
            try:
                async for _chunk in request.stream():
                    pass
            except ClientDisconnect:  # pragma: no cover
                pass
            return blocked
        try:
            async with request.form(
                max_files=world_store.max_upload_files, max_fields=MAX_UPLOAD_FIELDS
            ) as form:
                requested = form.get("name")
                parts: list[tuple[str, Any, int]] = []
                for _field, value in form.multi_items():
                    if isinstance(value, UploadFile):
                        parts.append((value.filename or "", value.file, _part_size(value)))
                return await run_in_threadpool(
                    _place_upload,
                    parts,
                    requested if isinstance(requested, str) else "",
                )
        # Starlette turns MultiPartException into an HTTPException whenever the scope
        # carries an app -- which FastAPI always sets -- so catching only the former
        # would send this guidance nowhere and answer in FastAPI's `detail` shape
        # instead of the refreshed panel every other refusal returns.
        except (MultiPartException, HTTPException) as exc:
            detail = getattr(exc, "message", None) or getattr(exc, "detail", "") or str(exc)
            return await run_in_threadpool(
                _upload_refused,
                400,
                f"That upload could not be read: {detail} An explored world is a "
                f"lot of small files and at most {world_store.max_upload_files} can go "
                "at once, so zip the world's folder and upload the .zip instead. "
                "Nothing was saved.",
            )
        except ClientDisconnect:
            # The browser went away mid-upload. Nothing was written -- placement only
            # happens after the whole body is in -- and nobody is left to read this.
            return await run_in_threadpool(
                _upload_refused, 400, "The upload stopped part-way. Nothing was saved."
            )

    async def _run_action(fn) -> JSONResponse:
        try:
            status = await run_in_threadpool(fn)
        except DockerControlError as exc:
            payload = exc.as_dict()
            payload["status"] = await run_in_threadpool(safe_status)
            return JSONResponse(payload, status_code=502)
        return JSONResponse({"status": status})

    # ---------------------------------------------------------- websocket

    @app.websocket("/ws/logs")
    async def ws_logs(websocket: WebSocket) -> None:
        # The HTTP setup gate does not see WebSocket scopes, so an unconfigured
        # manager has to refuse here in its own right.
        current = app.state.auth
        session = (
            current.read_token(websocket.cookies.get(current.cookie_name))
            if current is not None
            else None
        )
        if session is None:
            # Refuse before accepting: an unauthenticated client never sees a line.
            await websocket.close(code=1008)
            return
        await websocket.accept()
        pump = asyncio.create_task(_pump(websocket))
        drain = asyncio.create_task(_drain(websocket))
        done, pending = await asyncio.wait({pump, drain}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
                log.info("log websocket ended: %r", exc)

    async def _drain(websocket: WebSocket) -> None:
        """Consume client frames so a disconnect is noticed promptly."""
        while True:
            await websocket.receive_text()

    async def _pump(websocket: WebSocket) -> None:
        # Docker's `since` has whole-second resolution, so every poll re-reads the
        # final second it already delivered. A fixed-size "lines I have seen" set was
        # the wrong shape for that: SteamCMD's first-run progress output can emit more
        # lines in one second than the set holds, evicting exactly the entries the
        # re-read needs to match -- duplicates, at the moment the README tells the
        # operator to watch the console. Instead: drop anything older than the last
        # line emitted, and remember only the lines sharing that one second, which is
        # all a re-read can ever repeat and is self-limiting.
        high_epoch = 0.0
        seen_at_high: set[str] = set()
        since: float | None = None
        tick = 0
        while True:
            if tick % max(1, config.status_every_n_polls) == 0:
                # Authorising once at the handshake is not enough: a 7-day session can
                # expire while the socket is open, and it would keep streaming status
                # and log lines until the connection happened to drop. Re-validating
                # the handshake token catches expiry; the browser turns 1008 into a
                # redirect to /login. (A sign-out elsewhere cannot be seen here -- the
                # token stays valid until it expires; see README's trade-offs.)
                live = app.state.auth
                if (
                    live is None
                    or live.read_token(websocket.cookies.get(live.cookie_name)) is None
                ):
                    await websocket.close(code=1008)
                    return
                envelope = await run_in_threadpool(status_envelope)
                await websocket.send_json({"type": "status", **envelope})
            try:
                lines = await run_in_threadpool(
                    control.fetch_logs,
                    since=since,
                    tail="all" if since else config.log_tail_lines,
                )
            except DockerControlError as exc:
                await websocket.send_json({"type": "log_error", "error": exc.message})
                lines = []

            fresh: list[str] = []
            for line in lines:
                if line.epoch < high_epoch:
                    continue
                if line.epoch > high_epoch:
                    high_epoch = line.epoch
                    seen_at_high.clear()
                elif line.raw in seen_at_high:
                    continue
                seen_at_high.add(line.raw)
                fresh.append(line.message)
                if line.epoch:
                    # Re-request from the same whole second; duplicates are filtered above.
                    since = max(since or 0.0, line.epoch)
            if fresh:
                await websocket.send_json({"type": "log", "lines": fresh})
            tick += 1
            await asyncio.sleep(config.log_poll_seconds)

    return app


def _part_size(upload: UploadFile) -> int:
    """How many bytes a multipart part actually carried.

    ``UploadFile.size`` is set by the parser, but it is the one number the size cap
    rests on, so a parser that ever stops setting it must fall back to measuring
    rather than silently counting the part as nothing.
    """
    if upload.size is not None:
        return upload.size
    try:  # pragma: no cover - every supported Starlette sets .size
        here = upload.file.tell()
        upload.file.seek(0, os.SEEK_END)
        size = upload.file.tell()
        upload.file.seek(here)
        return size
    except (OSError, ValueError):
        return 0


def _udp_ports(store: SettingsStore) -> list[int]:
    """Game port, its query port, and the crossplay backend port."""
    base = store.server_port()
    return [base, base + 1, base + 2]


def _checkbox(value: str) -> bool:
    """An HTML checkbox is absent when unticked and arbitrary when ticked."""
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


# Each flag key's own spelling, as the upstream image documents it: SERVER_PUBLIC is
# 0/1 and CROSSPLAY is false/true, so a JSON boolean is rendered per key.
_FLAG_TEXT = {"SERVER_PUBLIC": ("0", "1"), "CROSSPLAY": ("false", "true")}

# Editable keys the panel builds rather than accepts: a value for one of these arrives
# as structured fields and is composed by the manager.
_COMPOSED_KEYS = frozenset({"SERVER_ARGS"})


def _submitted_modifiers(body: dict[str, Any]) -> dict[str, Any] | None:
    """The world-modifier fields this request carried, or ``None`` for "not touched".

    ``None`` and ``{}`` are different answers: a request with no ``modifiers`` object at
    all leaves ``SERVER_ARGS`` exactly as the file has it, while an empty object means
    every category is at its default and every toggle is off -- which composes to an
    empty value and clears the managed arguments.
    """
    raw = body.get("modifiers")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SetupInputError(
            "The world modifiers must be sent as an object of fields "
            f"({', '.join(MODIFIER_FIELD_KEYS)}), not {type(raw).__name__}."
        )
    return raw


def _submitted_settings(body: dict[str, Any]) -> dict[str, str]:
    """The editable keys this request actually carried, as env-file strings.

    Accepts ``{"settings": {...}}`` or a flat object. Keys outside ``SETTINGS_KEYS``
    are ignored rather than refused -- the panel is not the place to write ``PUID`` or
    the operator's own additions, and silently declining them is what keeps a mistyped
    key from becoming a new line in the file. A key the request does not carry at all
    is left alone entirely, which is how an untouched field stays untouched.

    A key it *does* carry with a value an env file cannot hold (``null``, a nested
    object, a fractional number) raises ``SetupInputError`` naming the field. Dropping
    it instead would answer "nothing changed" to an operator who plainly changed
    something.

    ``SERVER_ARGS`` is editable but not *submittable* here: it is composed from the
    ``modifiers`` fields (see ``_submitted_modifiers``), so a ready-made string in
    ``settings`` is ignored like any other key the panel does not own. Composing
    server-side is what makes the ordering rule and the preserved unmanaged arguments
    the manager's job rather than the browser's.
    """
    raw = body.get("settings")
    if not isinstance(raw, dict):
        raw = body
    submitted: dict[str, str] = {}
    for key in SETTINGS_KEYS:
        if key not in raw or key in _COMPOSED_KEYS:
            continue
        value = raw[key]
        if isinstance(value, bool):
            off, on = _FLAG_TEXT.get(key, ("false", "true"))
            value = on if value else off
        elif isinstance(value, int):
            value = str(value)
        elif not isinstance(value, str):
            kind = "null" if value is None else type(value).__name__
            raise SetupInputError(
                f"{key} was sent as {kind}, which is not a value the settings file can "
                "hold. Send it as text."
            )
        submitted[key] = value
    return submitted


def _refuse_doomed_start(store: SettingsStore) -> None:
    """Refuse a Start that could only produce a container that exits immediately.

    The wizard requires a join password, but the settings file is the operator's to
    edit afterwards, and ``valheim_server.x86_64`` refuses to start without one --
    whatever ``SERVER_PUBLIC`` says. Left to Docker, that shows up as "stopped /
    Container exited" with no cause anywhere in the UI, so name it here instead.
    """
    try:
        values = store.read()
    except SettingsFileError as exc:
        # The container env comes from this file, so an unreadable one is a failed
        # Start either way -- just with a worse message from the engine.
        raise ApiError(400, str(exc)) from exc
    if len(values.get("SERVER_PASS", "")) < MIN_SERVER_PASS_LENGTH:
        raise ApiError(
            400,
            f"SERVER_PASS in {store.path} is empty or shorter than "
            f"{MIN_SERVER_PASS_LENGTH} characters. The Valheim server refuses to start "
            "without a join password of at least that length, so the container would "
            "exit immediately. Set one in the settings file and press Start again.",
        )


def _log_setup_banner(config: AppConfig, setup: SetupSession) -> None:
    """Print the one thing a clean install cannot be completed without.

    Deliberately at WARNING and framed in rules: this line has to be findable in
    ``docker compose logs manager`` among SteamCMD's output. The token is the only
    secret here and it is short-lived by construction -- it exists only in memory, a
    restart mints a new one, and completing setup voids it.
    """
    rule = "=" * 74
    lines = [
        "",
        rule,
        " FIRST-RUN SETUP REQUIRED -- no admin account exists yet.",
        " Open this one-time URL to create one:",
        "",
        f"   {setup.setup_url(config.manager_url or 'http://<this-host>:8080')}",
        "",
    ]
    if not config.manager_url:
        lines += [
            " Replace <this-host> with the address you reach the manager on (set",
            " MANAGER_URL in .env to have this printed ready to click).",
        ]
    lines += [
        " This URL works once, only until setup completes, and a manager restart",
        " replaces it. Nothing else is reachable until then.",
        rule,
    ]
    log.warning("\n".join(lines))


# Served as an ASGI factory (`uvicorn --factory app.main:create_app`) so importing
# this module never builds an app -- the tests inject their own config.
__all__ = ["create_app", "AppConfig", "config_from_env", "ApiError", "ConfigError"]
