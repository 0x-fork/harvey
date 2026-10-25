"""Loop liveness: the dashboard must detect the loop without ever killing it.

On Windows ``os.kill(pid, 0)`` is TerminateProcess, so every test here
asserts os.kill is never called on that path. Nothing here signals a real
process other than the test runner itself (signal 0 on POSIX only).
"""

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
import harvey.pidfile as pidfile


def _no_kill(*args, **kwargs):
    raise AssertionError("os.kill must not be used as a liveness check here")


# ── pid_alive, per platform ──


def test_windows_live_pid_uses_kernel_query(monkeypatch):
    monkeypatch.setattr(pidfile.sys, "platform", "win32")
    monkeypatch.setattr(pidfile.os, "kill", _no_kill)
    monkeypatch.setattr(pidfile, "_pid_alive_windows", lambda pid: pid == 4242)
    assert pidfile.pid_alive(4242) is True


def test_windows_stale_pid_is_not_alive(monkeypatch):
    monkeypatch.setattr(pidfile.sys, "platform", "win32")
    monkeypatch.setattr(pidfile.os, "kill", _no_kill)
    monkeypatch.setattr(pidfile, "_pid_alive_windows", lambda pid: False)
    assert pidfile.pid_alive(16240) is False


def test_posix_live_pid(monkeypatch):
    calls = []
    monkeypatch.setattr(pidfile.sys, "platform", "linux")
    monkeypatch.setattr(pidfile.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    assert pidfile.pid_alive(4242) is True
    assert calls == [(4242, 0)]


@pytest.mark.parametrize("exc", [ProcessLookupError(), OSError(22, "Invalid argument")])
def test_posix_stale_pid(monkeypatch, exc):
    def _raise(pid, sig):
        raise exc
    monkeypatch.setattr(pidfile.sys, "platform", "linux")
    monkeypatch.setattr(pidfile.os, "kill", _raise)
    assert pidfile.pid_alive(16240) is False


def test_posix_other_users_process_counts_as_alive(monkeypatch):
    def _raise(pid, sig):
        raise PermissionError()
    monkeypatch.setattr(pidfile.sys, "platform", "linux")
    monkeypatch.setattr(pidfile.os, "kill", _raise)
    assert pidfile.pid_alive(1) is True


def test_nonpositive_pid_is_never_alive():
    assert pidfile.pid_alive(0) is False
    assert pidfile.pid_alive(-1) is False


@pytest.mark.skipif(sys.platform != "win32", reason="real kernel32 query")
def test_windows_real_query_on_self_and_unused_pid():
    # Querying is read-only: our own process stays alive afterwards.
    assert pidfile._pid_alive_windows(os.getpid()) is True
    assert pidfile._pid_alive_windows(0x7FFFFFF0) is False


# ── PID file lifecycle (harvey run) ──


def test_write_then_remove_own_pid(tmp_path):
    f = tmp_path / "data" / "harvey.pid"
    pidfile.write_pid_file(f)
    assert pidfile.read_pid(f) == os.getpid()
    pidfile.remove_pid_file(f)
    assert not f.exists()


def test_remove_leaves_another_loops_pid(tmp_path):
    f = tmp_path / "harvey.pid"
    f.write_text("999999")
    pidfile.remove_pid_file(f)
    assert f.read_text() == "999999"


# ── Dashboard status endpoint ──


@pytest.fixture
def status_client(monkeypatch, tmp_path):
    f = tmp_path / "harvey.pid"
    monkeypatch.setattr(dash, "PID_FILE", f)
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "harvey.db")
    monkeypatch.setattr(dash, "_harvey_process", None)
    monkeypatch.setattr(dash.os, "kill", _no_kill)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {token}"}) as c:
        c.pid_file = f
        yield c


def test_status_running_for_cli_started_loop(status_client, monkeypatch):
    """A loop started with `harvey run` has no dashboard subprocess, only its PID file."""
    status_client.pid_file.write_text("4242")
    monkeypatch.setattr(dash, "pid_alive", lambda pid: pid == 4242)
    data = status_client.get("/api/harvey/status").json()
    assert data["running"] is True
    assert data["pid"] == 4242


def test_status_stale_pid_reports_offline_and_cleans_up(status_client, monkeypatch):
    status_client.pid_file.write_text("16240")
    monkeypatch.setattr(dash, "pid_alive", lambda pid: False)
    data = status_client.get("/api/harvey/status").json()
    assert data["running"] is False
    assert not status_client.pid_file.exists()


def test_status_garbage_pid_file(status_client):
    status_client.pid_file.write_text("not a pid")
    assert status_client.get("/api/harvey/status").json()["running"] is False
    assert not status_client.pid_file.exists()
