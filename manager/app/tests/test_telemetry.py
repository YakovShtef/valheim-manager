"""Live CPU and memory for the Console's status bar.

Docker's stats endpoint is reachable through the socket proxy because it sits under
/containers, which the proxy already allows. These pin the arithmetic (the same
formula the Docker CLI uses) and that the endpoint degrades to "not available"
rather than erroring when stats cannot be read.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.docker_control import telemetry_from_stats
from app.tests.test_edge_cases import (  # noqa: F401  (fixtures)
    ADMIN_PASSWORD,
    ADMIN_USER,
    ORIGIN,
    env_file,
    fake_docker,
    login,
    stack,
)

GIB = 1024 ** 3

# Trimmed from a real `docker stats --no-stream` payload, cgroup v2.
STATS = {
    "cpu_stats": {
        "cpu_usage": {"total_usage": 2_000_000_000},
        "system_cpu_usage": 120_000_000_000,
        "online_cpus": 4,
    },
    "precpu_stats": {
        "cpu_usage": {"total_usage": 1_500_000_000},
        "system_cpu_usage": 116_000_000_000,
    },
    "memory_stats": {
        "usage": 3 * GIB,
        "limit": 16 * GIB,
        "stats": {"inactive_file": 1 * GIB},
    },
}


def test_cpu_is_the_share_of_the_whole_machine_times_its_cores():
    # 0.5s of CPU over 4s of system time on 4 cores: (0.5 / 4) * 4 * 100 = 50%.
    assert telemetry_from_stats(STATS)["cpu_percent"] == 50.0


def test_memory_leaves_out_reclaimable_file_cache():
    """What `docker stats` shows: usage minus the page cache the kernel can drop."""
    numbers = telemetry_from_stats(STATS)
    assert numbers["memory_used"] == 2 * GIB
    assert numbers["memory_limit"] == 16 * GIB


def test_cgroup_v1_cache_is_left_out_too():
    stats = {**STATS, "memory_stats": {"usage": 3 * GIB, "limit": 16 * GIB, "stats": {"cache": GIB}}}
    assert telemetry_from_stats(stats)["memory_used"] == 2 * GIB


def test_a_first_sample_with_no_previous_reading_has_no_cpu_figure():
    """Docker's first sample carries no precpu; inventing 0% would read as idle."""
    stats = {**STATS, "precpu_stats": {}}
    assert telemetry_from_stats(stats)["cpu_percent"] is None


def test_a_payload_with_nothing_usable_is_all_none():
    assert telemetry_from_stats({}) == {
        "cpu_percent": None, "memory_used": None, "memory_limit": None,
    }


def test_the_endpoint_needs_a_session(stack):
    with TestClient(stack["app"]) as client:
        assert client.get("/api/telemetry").status_code == 401


def test_the_endpoint_says_unavailable_when_there_is_no_server(stack):
    with TestClient(stack["app"]) as client:
        login(client)
        body = client.get("/api/telemetry").json()
    assert body == {"available": False, "cpu_percent": None, "memory_used": None,
                    "memory_limit": None}


def test_the_endpoint_reports_a_running_servers_numbers(stack, monkeypatch):
    class Running:
        id = "c1"
        attrs = {"State": {"Status": "running", "StartedAt": "2026-10-01T10:00:00Z"}}

        def stats(self, stream=False):
            return STATS

    control = stack["app"].state.control
    monkeypatch.setattr(control, "get_container", lambda: Running())
    with TestClient(stack["app"]) as client:
        login(client)
        body = client.get("/api/telemetry").json()
    assert body == {"available": True, "cpu_percent": 50.0, "memory_used": 2 * GIB,
                    "memory_limit": 16 * GIB}


def test_a_stats_failure_is_unavailable_not_an_error(stack, monkeypatch):
    class Broken:
        id = "c1"
        attrs = {"State": {"Status": "running"}}

        def stats(self, stream=False):
            raise RuntimeError("proxy said no")

    control = stack["app"].state.control
    monkeypatch.setattr(control, "get_container", lambda: Broken())
    with TestClient(stack["app"]) as client:
        login(client)
        response = client.get("/api/telemetry")
    assert response.status_code == 200
    assert response.json()["available"] is False
