"""Tests for systemd --user unit control (4.1 service enable/disable):
state probing across the environments we degrade in, and the enable/disable
wrappers. All subprocess calls are faked — no systemd needed to test."""

from __future__ import annotations

import subprocess

from kenzy import unitctl


class _Proc:
    def __init__(self, code: int, out: str = "", err: str = ""):
        self.returncode = code
        self.stdout = out
        self.stderr = err


def _fake_run(responses):
    """Map the systemctl subcommand ('is-enabled', 'disable', …) to a _Proc."""

    calls = []

    def run(cmd, **kwargs):
        assert cmd[:2] == ["systemctl", "--user"]
        calls.append(cmd[2:])
        return responses[cmd[2]]

    return run, calls


def test_no_systemctl_degrades_honestly(monkeypatch):
    monkeypatch.setattr(unitctl.shutil, "which", lambda _: None)
    state = unitctl.unit_state("kenzy-llm.service")
    assert state == {"systemd": False, "exists": False, "enabled": False, "active": False}


def test_unit_not_found(monkeypatch):
    monkeypatch.setattr(unitctl.shutil, "which", lambda _: "/bin/systemctl")
    run, _ = _fake_run({"is-enabled": _Proc(1, err="Failed to get unit file state: not-found")})
    monkeypatch.setattr(unitctl.subprocess, "run", run)
    state = unitctl.unit_state("kenzy-llm.service")
    assert state["systemd"] is True and state["exists"] is False


def test_no_user_manager_reads_as_no_systemd(monkeypatch):
    # Containers/dev shells: systemctl exists but there's no user manager.
    monkeypatch.setattr(unitctl.shutil, "which", lambda _: "/bin/systemctl")
    run, _ = _fake_run({"is-enabled": _Proc(1, err="Failed to connect to bus")})
    monkeypatch.setattr(unitctl.subprocess, "run", run)
    assert unitctl.unit_state("x.service")["systemd"] is False


def test_enabled_and_active(monkeypatch):
    monkeypatch.setattr(unitctl.shutil, "which", lambda _: "/bin/systemctl")
    run, calls = _fake_run(
        {"is-enabled": _Proc(0, out="enabled"), "is-active": _Proc(0, out="active")}
    )
    monkeypatch.setattr(unitctl.subprocess, "run", run)
    state = unitctl.unit_state("kenzy-llm.service")
    assert state == {"systemd": True, "exists": True, "enabled": True, "active": True}
    assert calls[0][0] == "is-enabled" and calls[1][0] == "is-active"


def test_disabled_but_running(monkeypatch):
    # `systemctl start` without enable: running now, won't survive a reboot.
    monkeypatch.setattr(unitctl.shutil, "which", lambda _: "/bin/systemctl")
    run, _ = _fake_run(
        {"is-enabled": _Proc(1, out="disabled"), "is-active": _Proc(0, out="active")}
    )
    monkeypatch.setattr(unitctl.subprocess, "run", run)
    state = unitctl.unit_state("kenzy-llm.service")
    assert state["exists"] is True and state["enabled"] is False and state["active"] is True


def test_disable_and_enable_report_outcome(monkeypatch):
    monkeypatch.setattr(unitctl.shutil, "which", lambda _: "/bin/systemctl")
    run, calls = _fake_run(
        {"disable": _Proc(0, out="Removed symlink."), "enable": _Proc(1, err="boom")}
    )
    monkeypatch.setattr(unitctl.subprocess, "run", run)
    ok, msg = unitctl.disable_unit("kenzy-llm.service")
    assert ok is True and calls[0] == ["disable", "--now", "kenzy-llm.service"]
    ok, msg = unitctl.enable_unit("kenzy-llm.service")
    assert ok is False and "boom" in msg


def test_timeout_never_raises(monkeypatch):
    monkeypatch.setattr(unitctl.shutil, "which", lambda _: "/bin/systemctl")

    def hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, unitctl._TIMEOUT)

    monkeypatch.setattr(unitctl.subprocess, "run", hang)
    state = unitctl.unit_state("kenzy-llm.service")  # degrades, no exception
    assert state["systemd"] is True
    assert state["enabled"] is False and state["active"] is False
    ok, msg = unitctl.disable_unit("kenzy-llm.service")
    assert ok is False and msg
