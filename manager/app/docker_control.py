"""Docker Engine API wrapper, pointed at the scoped socket proxy.

Never uses ``docker.from_env()`` and never shells out to the ``docker`` CLI: the
only reachable endpoint is ``DOCKER_HOST=tcp://docker-socket-proxy:2375``, and the
proxy allowlists just container inspect/create/start/stop/remove/logs plus image
inspect/pull.

Two things this module is careful about:

* *container running* and *server ready* are different facts. Readiness comes
  only from the Valheim server's own stdout line, so the UI can say
  "loading world" instead of lying.
* a failed first-run start must not leave a half-created container behind.
"""

from __future__ import annotations

import calendar
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound

log = logging.getLogger(__name__)

# Phases the WebUI renders. "absent" means the container has never been created.
PHASE_ABSENT = "absent"
PHASE_PULLING = "pulling"
PHASE_CREATING = "creating"
PHASE_STARTING = "starting"
PHASE_RUNNING = "running"
PHASE_READY = "ready"
PHASE_STOPPING = "stopping"
PHASE_STOPPED = "stopped"
PHASE_PAUSED = "paused"

MANAGED_LABEL = "com.valheim-manager.managed"

# Container states that mean the server is on, each with what to call it when
# refusing. Editing settings is refused for all three: "paused" is a live process with
# the world loaded, and "restarting" is on its way back up. Docker itself refuses to
# remove a container in any of them.
_LIVE_REASON = {
    "running": "The server is running.",
    "restarting": "The server is restarting.",
    "paused": "The server is paused.",
}

# Derived, never hand-synced: a state added above without its sentence would otherwise
# turn a refusal into a KeyError, which is a 500 on the settings save route.
LIVE_STATES = tuple(_LIVE_REASON)

# Manager-side phases that mean a start or stop is already in flight. Not a container
# state, but just as much a reason not to edit the environment a create is reading.
_IN_FLIGHT_PHASES = (PHASE_PULLING, PHASE_CREATING, PHASE_STARTING, PHASE_STOPPING)

# The advice that follows each reason. They differ on purpose: mid-pull there is
# nothing running to stop yet, and mid-stop the operator has already asked for it and
# only has to wait.
STOP_FIRST = "Turn the server off first, then try again."
WAIT_FIRST = "Give it a moment, then try again."


class DockerControlError(RuntimeError):
    """A Docker operation failed; ``docker_message`` is the engine's own text."""

    def __init__(self, message: str, docker_message: str = "", *, force_available: bool = False):
        super().__init__(message)
        self.message = message
        self.docker_message = docker_message
        self.force_available = force_available

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": self.message,
            "docker_error": self.docker_message,
            "force_available": self.force_available,
        }


@dataclass(frozen=True)
class LogLine:
    """One log record. ``raw`` includes the engine timestamp so it dedupes cleanly."""

    epoch: float
    raw: str
    message: str


# Safety-net TTLs for a transient phase, in case the thread holding it dies without
# clearing it. A first-run pull fetches the better part of a gigabyte over whatever
# link the operator has, so it gets a far longer leash than create/start: expiring
# mid-pull would report "absent", re-enable Start, and let a second create 409.
_ACTION_TTL = {
    PHASE_PULLING: 6 * 3600.0,
    PHASE_CREATING: 600.0,
    PHASE_STARTING: 600.0,
}
_DEFAULT_ACTION_TTL = 900.0


@dataclass
class _Action:
    """A transient manager-side phase (pull/create/start) Docker cannot report."""

    phase: str
    message: str
    ttl: float = _DEFAULT_ACTION_TTL
    expires_at: float = field(default=0.0)

    def __post_init__(self) -> None:
        self.touch()

    def touch(self) -> None:
        self.expires_at = time.monotonic() + self.ttl

    def alive(self) -> bool:
        return time.monotonic() < self.expires_at


def _split_image_ref(image: str) -> tuple[str, str]:
    """Split an image reference into what ``images.pull`` wants as repo + tag.

    Handles the three shapes a ``VALHEIM_IMAGE`` can take:
    ``repo`` -> ``latest``, ``repo:tag`` -> that tag, and ``repo@sha256:...`` -> the
    digest as the "tag" (which is how the SDK pulls by digest). A registry port in
    ``host:5000/repo`` must not be mistaken for a tag.
    """
    repo, at, digest = image.partition("@")
    if at:
        return repo, digest
    head, sep, tail = image.rpartition(":")
    if sep and "/" not in tail:
        return head, tail
    return image, "latest"


def _parse_log_chunk(chunk: bytes) -> list[LogLine]:
    lines: list[LogLine] = []
    for raw_bytes in chunk.splitlines():
        raw = raw_bytes.decode("utf-8", errors="replace").rstrip("\r")
        if not raw:
            continue
        stamp, _, message = raw.partition(" ")
        lines.append(LogLine(epoch=_epoch_from_stamp(stamp), raw=raw, message=message or stamp))
    return lines


def _epoch_from_stamp(stamp: str) -> float:
    """RFC3339 nano timestamp -> epoch seconds. 0.0 when unparseable."""
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?Z?", stamp)
    if not match:
        return 0.0
    y, mo, d, h, mi, s = (int(match.group(i)) for i in range(1, 7))
    frac = match.group(7) or "0"
    try:
        base = calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))
    except (ValueError, OverflowError):  # pragma: no cover
        return 0.0
    return base + float("0." + frac)


class DockerControl:
    """Everything the manager needs from the Docker Engine, and nothing more."""

    def __init__(
        self,
        *,
        base_url: str,
        container_name: str,
        image: str,
        network: str | None,
        config_volume: str,
        data_volume: str,
        env_provider: Callable[[], dict[str, str]],
        port_provider: Callable[[], list[int]],
        ready_pattern: str,
        stop_timeout: int = 120,
        restart_policy: str = "unless-stopped",
        cap_add: Iterable[str] = ("sys_nice",),
        ready_scan_lines: int = 400,
        ready_rescan_seconds: float = 30.0,
        api_timeout: int = 60,
        client_factory: Callable[[], Any] | None = None,
    ):
        self.base_url = base_url
        self.container_name = container_name
        self.image = image
        self.network = network or None
        self.config_volume = config_volume
        self.data_volume = data_volume
        self.env_provider = env_provider
        self.port_provider = port_provider
        self.ready_re = re.compile(ready_pattern, re.IGNORECASE)
        self.ready_pattern = ready_pattern
        self.stop_timeout = stop_timeout
        self.restart_policy = restart_policy
        self.cap_add = list(cap_add)
        self.ready_scan_lines = ready_scan_lines
        self.ready_rescan_seconds = ready_rescan_seconds
        self.api_timeout = api_timeout
        self._client_factory = client_factory or self._default_client_factory
        self._client: Any = None
        self._client_lock = threading.Lock()
        self._action_lock = threading.Lock()
        self._action: _Action | None = None
        # Readiness is remembered per container *run* -- the key carries StartedAt,
        # so a restart automatically drops back to "not yet ready".
        self._ready_runs: set[tuple[str, str]] = set()
        # When each run was last scanned and came back NOT ready. Only the positive
        # answer is permanent; without this the negative one re-reads the whole run's
        # log on every status poll, forever, for a server whose ready line never
        # arrives -- per open browser tab, against a log with no size cap.
        self._ready_scanned_at: dict[tuple[str, str], float] = {}

    # ---------------------------------------------------------------- client

    def _default_client_factory(self) -> Any:
        # Explicit base_url: the proxy, never the host socket, never from_env().
        return docker.DockerClient(base_url=self.base_url, timeout=self.api_timeout)

    def client(self) -> Any:
        with self._client_lock:
            if self._client is None:
                try:
                    self._client = self._client_factory()
                except DockerException as exc:
                    raise DockerControlError(
                        "Cannot reach the Docker socket proxy.", str(exc)
                    ) from exc
            return self._client

    def _reset_client(self) -> None:
        with self._client_lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:  # pragma: no cover - best effort
                pass

    # ------------------------------------------------------------- container

    def get_container(self) -> Any | None:
        """The managed container, or ``None`` when it does not exist."""
        try:
            return self.client().containers.get(self.container_name)
        except NotFound:
            return None
        except APIError as exc:
            raise DockerControlError("Docker rejected the container inspect.", str(exc)) from exc
        except DockerControlError:
            raise
        except Exception as exc:
            # DockerException plus the transport errors that are not one of them:
            # requests' ReadTimeout / ConnectionError from a slow or hung proxy.
            self._reset_client()
            raise DockerControlError("Lost contact with the Docker socket proxy.", str(exc)) from exc

    # ----------------------------------------------------------------- state

    def _set_action(self, phase: str, message: str) -> None:
        ttl = _ACTION_TTL.get(phase, _DEFAULT_ACTION_TTL)
        if phase == PHASE_STOPPING:
            # The engine itself waits stop_timeout before SIGKILL.
            ttl = self.stop_timeout + 300.0
        with self._action_lock:
            self._action = _Action(phase=phase, message=message, ttl=ttl)

    def _clear_action(self) -> None:
        with self._action_lock:
            self._action = None

    def _current_action(self) -> _Action | None:
        with self._action_lock:
            if self._action is not None and not self._action.alive():
                self._action = None
            return self._action

    def _run_key(self, container: Any) -> tuple[str, str]:
        state = (container.attrs or {}).get("State", {}) or {}
        return (container.id or "", str(state.get("StartedAt", "")))

    def mark_ready_if_match(self, container: Any, lines: Iterable[LogLine]) -> bool:
        """Flip readiness when any of ``lines`` carries the server's listening line.

        Lines older than this run's ``StartedAt`` are ignored. They do arrive: the log
        pump carries a ``since`` from before a restart, so without this filter the
        *previous* run's ready line marks a freshly restarted, still-loading server as
        ready -- the exact conflation this module exists to avoid.
        """
        key = self._run_key(container)
        if key in self._ready_runs:
            return True
        started = _epoch_from_stamp(key[1])
        for line in lines:
            if started > 0 and line.epoch and line.epoch < started:
                continue
            if self.ready_re.search(line.message) or self.ready_re.search(line.raw):
                self._ready_runs.add(key)
                self._ready_scanned_at.pop(key, None)
                return True
        return False

    def _is_ready(self, container: Any) -> bool:
        key = self._run_key(container)
        if key in self._ready_runs:
            return True
        last_scan = self._ready_scanned_at.get(key)
        if last_scan is not None and time.monotonic() - last_scan < self.ready_rescan_seconds:
            # Already looked, recently, and the whole run was in that look. The log
            # pump calls mark_ready_if_match on every new line, so a ready line that
            # arrives meanwhile still flips this within a poll for anyone watching.
            return False
        # No stream has seen it yet (e.g. nobody had the UI open, or the manager was
        # restarted). Scan the whole of THIS run -- `since=StartedAt` already bounds
        # it. A line cap would lose the ready line on a server that has been up long
        # enough to push it out of the window, leaving the UI stuck on "not yet ready"
        # forever; only an unbounded run has to fall back to the cap.
        started = (container.attrs or {}).get("State", {}).get("StartedAt")
        since = _epoch_from_stamp(started or "")
        try:
            chunk = container.logs(
                stdout=True,
                stderr=True,
                timestamps=True,
                tail="all" if since > 0 else self.ready_scan_lines,
                since=int(since) if since > 0 else None,
            )
        except Exception as exc:
            log.warning("readiness scan failed: %s", exc)
            return False
        if self.mark_ready_if_match(container, _parse_log_chunk(chunk or b"")):
            return True
        self._ready_scanned_at[key] = time.monotonic()
        return False

    def status(self) -> dict[str, Any]:
        """Container truth + manager-side transient phase, merged for the UI."""
        action = self._current_action()
        container = self.get_container()

        if container is None:
            phase = action.phase if action else PHASE_ABSENT
            return {
                "phase": phase,
                "container_exists": False,
                "container_state": None,
                "ready": False,
                "image": self.image,
                "container_name": self.container_name,
                "started_at": None,
                "exit_code": None,
                "message": action.message if action else "No server container yet.",
                "ready_pattern": self.ready_pattern,
            }

        state = (container.attrs or {}).get("State", {}) or {}
        docker_state = str(state.get("Status") or "unknown")
        ready = docker_state == "running" and self._is_ready(container)

        if docker_state == "running":
            phase = PHASE_READY if ready else PHASE_RUNNING
            message = (
                "Server is ready for connections."
                if ready
                else "Container running, world still loading (not yet ready)."
            )
            # A running container outranks any leftover pull/create/start phase.
            if action and action.phase in (PHASE_PULLING, PHASE_CREATING, PHASE_STARTING):
                self._clear_action()
                action = None
        elif docker_state == "restarting":
            phase, message = PHASE_STARTING, "Container restarting."
        elif docker_state == "paused":
            phase, message = PHASE_PAUSED, "Container paused."
        elif docker_state == "removing":
            phase, message = PHASE_STOPPING, "Container being removed."
        elif docker_state == "created":
            phase, message = PHASE_STOPPED, "Container created but never started."
        else:  # exited / dead / unknown
            phase, message = PHASE_STOPPED, f"Container {docker_state}."

        if action and action.phase in (PHASE_PULLING, PHASE_CREATING, PHASE_STARTING, PHASE_STOPPING):
            phase, message = action.phase, action.message

        return {
            "phase": phase,
            "container_exists": True,
            "container_state": docker_state,
            "ready": ready,
            "image": self.image,
            "container_name": self.container_name,
            "started_at": state.get("StartedAt"),
            "exit_code": state.get("ExitCode"),
            "message": message,
            "ready_pattern": self.ready_pattern,
        }

    # --------------------------------------------------------------- actions

    def _ensure_image(self) -> None:
        client = self.client()
        try:
            client.images.get(self.image)
            return
        except ImageNotFound:
            pass
        except APIError as exc:
            raise DockerControlError("Docker rejected the image inspect.", str(exc)) from exc

        repository, tag = _split_image_ref(self.image)
        self._set_action(PHASE_PULLING, f"Pulling {self.image} (this can take a while)...")
        try:
            client.images.pull(repository, tag=tag)
        except Exception as exc:
            raise DockerControlError(f"Could not pull {self.image}.", str(exc)) from exc

    def _create_container(self) -> Any:
        self._set_action(PHASE_CREATING, "Creating the Valheim container...")
        try:
            environment = self.env_provider()
        except Exception as exc:
            raise DockerControlError("Could not read the Valheim settings env file.", str(exc)) from exc

        ports = {f"{port}/udp": port for port in self.port_provider()}
        volumes = {
            self.config_volume: {"bind": "/config", "mode": "rw"},
            self.data_volume: {"bind": "/opt/valheim", "mode": "rw"},
        }
        restart_policy = (
            {"Name": self.restart_policy} if self.restart_policy and self.restart_policy != "no" else None
        )
        try:
            # No `stop_timeout=` here. The Engine accepts it, but docker-py's *model*
            # layer does not: it validates kwargs against RUN_CREATE_KWARGS /
            # RUN_HOST_CONFIG_KWARGS and raises "run() got an unexpected keyword
            # argument 'stop_timeout'" before any request is sent. Nothing is lost --
            # `stop()` passes `timeout=self.stop_timeout` on every call, so the grace
            # period this manager uses is unchanged. It only means a *manual*
            # `docker stop valheim-server` gets the Engine's 10s default instead.
            return self.client().containers.create(
                self.image,
                name=self.container_name,
                environment=environment,
                ports=ports,
                volumes=volumes,
                cap_add=self.cap_add,
                restart_policy=restart_policy,
                network=self.network,
                labels={MANAGED_LABEL: "true"},
                tty=False,
            )
        except Exception as exc:
            raise DockerControlError("Could not create the Valheim container.", str(exc)) from exc

    def _remove_container(self, container: Any) -> None:
        try:
            container.remove(force=True)
        except Exception as exc:  # pragma: no cover - cleanup is best effort
            # Must not mask the original start failure we are cleaning up after.
            log.warning("could not remove half-created container: %s", exc)

    def start(self) -> dict[str, Any]:
        """Pull if absent, create if absent, then start. Cleans up on failure."""
        try:
            container = self.get_container()
            created_here = False
            if container is None:
                self._ensure_image()
                container = self._create_container()
                created_here = True

            state = (container.attrs or {}).get("State", {}) or {}
            if state.get("Status") == "running":
                self._clear_action()
                return self.status()

            self._set_action(PHASE_STARTING, "Starting the container...")
            try:
                container.start()
            except Exception as exc:
                if created_here:
                    # "leaves no half-created container"
                    self._remove_container(container)
                raise DockerControlError(
                    "Could not start the Valheim container.", str(exc)
                ) from exc

            try:
                container.reload()
            except Exception as exc:
                raise DockerControlError(
                    "Started the container but could not re-inspect it.", str(exc)
                ) from exc
            self._clear_action()
            return self.status()
        except Exception:
            # Covers DockerControlError too -- never leave a stale transient phase.
            self._clear_action()
            raise

    def stop(self, *, force: bool = False) -> dict[str, Any]:
        container = self.get_container()
        if container is None:
            raise DockerControlError("There is no Valheim container to stop.")
        state = (container.attrs or {}).get("State", {}) or {}
        if state.get("Status") not in LIVE_STATES:
            self._clear_action()
            return self.status()

        self._set_action(
            PHASE_STOPPING,
            "Killing the container..." if force else f"Stopping gracefully (SIGTERM, {self.stop_timeout}s grace)...",
        )
        try:
            if force:
                container.kill()
            else:
                # Engine sends SIGTERM, waits `timeout`, then SIGKILL.
                container.stop(timeout=self.stop_timeout)
        except (APIError, DockerException) as exc:
            self._clear_action()
            raise DockerControlError(
                "Graceful stop did not complete in time." if not force else "Force stop failed.",
                str(exc),
                force_available=not force,
            ) from exc
        except Exception as exc:  # read timeout from the HTTP client
            self._clear_action()
            raise DockerControlError(
                "Graceful stop did not complete in time.",
                str(exc),
                force_available=not force,
            ) from exc
        self._clear_action()
        try:
            container.reload()
        except Exception as exc:
            raise DockerControlError(
                "Stopped the container but could not re-inspect it.", str(exc)
            ) from exc
        return self.status()

    def restart(self) -> dict[str, Any]:
        container = self.get_container()
        if container is None:
            # Nothing to restart yet -- the first run *is* a start.
            return self.start()
        state = (container.attrs or {}).get("State", {}) or {}
        if state.get("Status") in LIVE_STATES:
            self.stop()
        return self.start()

    # ------------------------------------------------- settings-apply support

    def running_reason(self) -> str | None:
        """Why the server counts as *on*, or ``None`` when it is off.

        "Off" is: no container at all, or one that is not running, restarting or
        paused, with no start or stop of the manager's own in flight. The settings
        panel asks this both to decide whether editing is allowed and to re-check when
        a save arrives -- the browser's view of the phase can be two seconds stale, and
        the server may have been started from somewhere else entirely in between.

        The answer is a complete sentence including what to do about it, because the
        two cases need different advice.
        """
        action = self._current_action()
        if action is not None and action.phase in _IN_FLIGHT_PHASES:
            return f"The server is busy: {action.message} {WAIT_FIRST}"
        container = self.get_container()
        if container is None:
            return None
        state = str((container.attrs or {}).get("State", {}).get("Status") or "")
        if state in LIVE_STATES:
            return f"{_LIVE_REASON[state]} {STOP_FIRST}"
        return None

    def remove_stopped_container(self) -> bool:
        """Remove the stopped container so the next Start creates a fresh one.

        Returns ``True`` when a container was removed and ``False`` when there was none
        to remove -- including one that disappeared underneath us, since the end state
        the caller wanted is the one it gets. Raises ``DockerControlError`` rather than
        removing a container that is running, restarting or paused.

        This is the whole point of the settings panel: ``start()`` creates a container
        only when none exists, so a saved settings file with the old container still
        around would leave the next Start running the old environment. Only the
        container goes -- the world, the backups and the server install live on the
        ``valheim-config`` / ``valheim-data`` volumes, which are named and never touched
        here.
        """
        container = self.get_container()
        if container is None:
            return False
        state = str((container.attrs or {}).get("State", {}).get("Status") or "")
        if state in LIVE_STATES:
            raise DockerControlError(
                f"{_LIVE_REASON[state]} The container can only be removed while it is "
                "stopped, so nothing was removed."
            )
        removed = True
        try:
            # Never force: a force removal is how a *running* container gets killed, and
            # the state check above is not a race this should be allowed to win.
            container.remove(force=False)
        except NotFound:
            # Removed on the host, or by a racing save, between the inspect and here.
            # The wanted end state -- no container -- is the one we have, so reporting a
            # failure would send the operator to `docker rm` for a container that is
            # already gone.
            removed = False
        except Exception as exc:
            raise DockerControlError(
                "Could not remove the stopped Valheim container.", str(exc)
            ) from exc
        # Readiness is remembered per container run; that container no longer exists.
        container_id = container.id or ""
        self._ready_runs = {key for key in self._ready_runs if key[0] != container_id}
        self._ready_scanned_at = {
            key: at for key, at in self._ready_scanned_at.items() if key[0] != container_id
        }
        return removed

    # ------------------------------------------------------------------ logs

    def fetch_logs(self, *, since: float | None = None, tail: int | str = "all") -> list[LogLine]:
        """One non-following read of the container log, newest last.

        Polling with ``since`` instead of a followed stream keeps every read
        cancellable (no orphaned blocking threads) while still landing new lines
        within a second of being emitted.
        """
        container = self.get_container()
        if container is None:
            return []
        try:
            chunk = container.logs(
                stdout=True,
                stderr=True,
                timestamps=True,
                tail=tail,
                since=int(since) if since and since > 0 else None,
            )
        except NotFound:
            return []
        except Exception as exc:
            # Includes requests' ReadTimeout / ConnectionError, which are not
            # DockerException -- letting one escape would silently kill the pump.
            raise DockerControlError("Could not read the container log.", str(exc)) from exc
        lines = _parse_log_chunk(chunk or b"")
        if lines:
            self.mark_ready_if_match(container, lines)
        return lines
