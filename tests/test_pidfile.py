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
    lock = status_client.pid_file.with_name("harvey.lock")
    monkeypatch.setattr(dash, "LOCK_FILE", lock)
    status_client.pid_file.write_text("4242")
    monkeypatch.setattr(dash, "pid_alive", lambda pid: pid == 4242)
    held = pidfile.hold_loop_lock(lock)
    try:
        data = status_client.get("/api/harvey/status").json()
    finally:
        pidfile.release_loop_lock(held)
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


# ── The loop lock: a PID file alone can outlive the loop ──
#
# After a crash or a reboot data/harvey.pid can name a process that is not
# Harvey at all (PIDs are reused). Only the running loop holds the lock, so
# a live PID with no lock held is somebody else's process.


def test_the_lock_is_held_only_while_a_loop_holds_it(tmp_path):
    lock = tmp_path / "harvey.lock"
    assert pidfile.loop_lock_held(lock) is False
    held = pidfile.hold_loop_lock(lock)
    assert held is not None
    assert pidfile.loop_lock_held(lock) is True
    pidfile.release_loop_lock(held)
    assert pidfile.loop_lock_held(lock) is False


def test_a_second_loop_cannot_take_the_lock(tmp_path):
    lock = tmp_path / "harvey.lock"
    first = pidfile.hold_loop_lock(lock)
    try:
        assert pidfile.hold_loop_lock(lock) is None
    finally:
        pidfile.release_loop_lock(first)


def test_probing_the_lock_does_not_take_it(tmp_path):
    lock = tmp_path / "harvey.lock"
    pidfile.loop_lock_held(lock)
    held = pidfile.hold_loop_lock(lock)
    assert held is not None
    pidfile.release_loop_lock(held)


def test_a_reused_pid_is_not_reported_as_harvey(status_client, monkeypatch, tmp_path):
    """The PID is alive, but it is some other program: no loop holds the lock."""
    monkeypatch.setattr(dash, "LOCK_FILE", tmp_path / "harvey.lock")
    status_client.pid_file.write_text("4242")
    monkeypatch.setattr(dash, "pid_alive", lambda pid: True)
    data = status_client.get("/api/harvey/status").json()
    assert data["running"] is False
    assert not status_client.pid_file.exists()


def test_stop_never_signals_a_reused_pid(status_client, monkeypatch, tmp_path):
    monkeypatch.setattr(dash, "LOCK_FILE", tmp_path / "harvey.lock")
    status_client.pid_file.write_text("4242")
    monkeypatch.setattr(dash, "pid_alive", lambda pid: True)
    # dash.os.kill is _no_kill: any signal fails the test.
    assert status_client.post("/api/harvey/stop").json()["success"] is False


def test_a_cli_loop_holding_the_lock_is_running(status_client, monkeypatch, tmp_path):
    lock = tmp_path / "harvey.lock"
    monkeypatch.setattr(dash, "LOCK_FILE", lock)
    status_client.pid_file.write_text("4242")
    monkeypatch.setattr(dash, "pid_alive", lambda pid: pid == 4242)
    held = pidfile.hold_loop_lock(lock)
    try:
        data = status_client.get("/api/harvey/status").json()
    finally:
        pidfile.release_loop_lock(held)
    assert data["running"] is True and data["pid"] == 4242


def test_harvey_run_refuses_when_a_loop_is_already_running(monkeypatch, tmp_path, capsys):
    import harvey.main as main_mod
    lock = tmp_path / "harvey.lock"
    monkeypatch.setattr(main_mod, "LOCK_FILE", lock)
    monkeypatch.setattr(main_mod, "_needs_setup", lambda: False)
    ran = []
    monkeypatch.setattr(main_mod, "_run_with_signals", lambda: ran.append(1))
    held = pidfile.hold_loop_lock(lock)
    try:
        with pytest.raises(SystemExit) as exc:
            main_mod.main()
    finally:
        pidfile.release_loop_lock(held)
    assert exc.value.code == 1
    assert ran == []
    assert "already running" in capsys.readouterr().out


# ── Dashboard Start ──


def test_start_launches_the_heartbeat_not_the_cli_help(status_client, monkeypatch, tmp_path):
    monkeypatch.setattr(dash, "LOCK_FILE", tmp_path / "harvey.lock")
    monkeypatch.setattr(dash, "LOG_FILE", tmp_path / "harvey.log")
    launched = {}

    class FakePopen:
        pid = 4242

        def __init__(self, cmd, **kwargs):
            launched["cmd"] = cmd
            launched["env"] = kwargs.get("env")

        def poll(self):
            return None

    monkeypatch.setattr(dash.subprocess, "Popen", FakePopen)
    assert status_client.post("/api/harvey/start").json()["success"] is True
    assert launched["cmd"][1:] == ["-m", "harvey", "run"]
