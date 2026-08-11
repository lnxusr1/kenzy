"""Joint core+plugin upgrades (the 5.1 version-skew defense, layer 1).

Pip alone protects nothing: upgrading core past a plugin's cap strands an
incompatible pair with a warning and exit 0, and upgrading a plugin can drag
core forward silently. Every Kenzy-driven upgrade therefore hands the resolver
core AND the installed plugin distributions in one invocation. These tests pin
that the set is actually assembled everywhere — command builders, the async
runner, and kenzy-deploy's remote sweep — and that a resolution failure states
the way out. The resolver's own hold-back/fail-before-change behavior is pip
semantics, exercised for real in the lab, not restated here.
"""

from __future__ import annotations

from typing import Any

from kenzy.deploy.deploy import HostConfig, _pip_target, _remote_plugin_dists
from kenzy.plugins import installed_plugin_dists
from kenzy.upgrade import joint_failure_note, pip_upgrade_command, run_pip_upgrade


def _host(**kw: Any) -> HostConfig:
    base: dict[str, Any] = dict(
        name="pi",
        address="pi.lan",
        ssh_user="pi",
        install_path="/opt/kenzy",
        venv_path="/opt/kenzy/.venv",
        python_bin="python3",
    )
    base.update(kw)
    return HostConfig(**base)


class _Dist:
    def __init__(self, name: str) -> None:
        self.name = name
        self.version = "1.0.0"


class _EP:
    def __init__(self, group: str, dist: str) -> None:
        self.group = group
        self.name = dist
        self.dist = _Dist(dist)


# ---------------------------------------------------------------------------
# Enumeration: which distributions ride the joint set
# ---------------------------------------------------------------------------


def test_enumeration_includes_incompatible_and_drops_garbage() -> None:
    """An api-incompatible plugin MUST ride the joint upgrade — moving core
    without it would freeze the very skew the load gate is reporting. Names
    that couldn't be a distribution never reach a pip argv."""
    eps = [
        _EP("kenzy.plugins.v1", "kenzy-ld2450"),
        _EP("kenzy.plugins.v99", "kenzy-future"),  # refused by the gate, still moved
        _EP("kenzy.plugins.v1", "kenzy-ld2450"),  # dedup
        _EP("kenzy.plugins.v1", "bad name; rm -rf /"),  # never rides argv
        _EP("kenzy.plugins.v1", "?"),  # no dist metadata
    ]
    assert installed_plugin_dists(eps) == ["kenzy-future", "kenzy-ld2450"]


# ---------------------------------------------------------------------------
# The command builders assemble the set
# ---------------------------------------------------------------------------


def test_upgrade_command_carries_the_plugin_set(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    cmd = pip_upgrade_command("node", None, plugins=["kenzy-ld2450", "kenzy-vision"])
    assert cmd[-3:] == ["kenzy[node]>=3.0.0", "kenzy-ld2450", "kenzy-vision"]


def test_upgrade_command_enumerates_by_default(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    monkeypatch.setattr("kenzy.plugins.installed_plugin_dists", lambda: ["kenzy-ld2450"])
    cmd = pip_upgrade_command("server", "5.1.0")
    assert cmd[-2:] == ["kenzy[server]==5.1.0", "kenzy-ld2450"]


def test_deploy_target_carries_the_plugin_set_in_both_modes() -> None:
    host = _host(install_mode="pypi")
    pypi = _pip_target(
        host, "node", upgrade=True, constraints="/c.txt", plugins=("kenzy-ld2450",)
    )
    assert pypi.endswith("'kenzy[node]>=3.0.0' 'kenzy-ld2450'")
    assert pypi.startswith("-c '/c.txt' -U ")
    src = _pip_target(_host(install_mode="source"), "node", upgrade=True, plugins=("kenzy-ld2450",))
    assert src.endswith("' 'kenzy-ld2450'") and "-e '" in src
    # No plugins → byte-identical to the pre-5.1 target (the sweep is unchanged
    # for every host with no add-ons).
    assert _pip_target(host, "node", upgrade=True) == "-U 'kenzy[node]>=3.0.0'"


# ---------------------------------------------------------------------------
# The async runner: joint set + a failure that states the way out
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, rc: int, out: bytes) -> None:
        self.returncode = rc
        self._out = out

    async def communicate(self) -> tuple[bytes, None]:
        return (self._out, None)


async def test_run_pip_upgrade_is_joint_and_annotates_resolution_failure(
    tmp_path: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    monkeypatch.setattr("kenzy.plugins.installed_plugin_dists", lambda: ["kenzy-ld2450"])
    captured: dict[str, Any] = {}

    async def _fake_exec(*cmd: str, **kw: Any) -> _FakeProc:
        captured["cmd"] = list(cmd)
        return _FakeProc(1, b"ERROR: ResolutionImpossible: for help visit ...")

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)
    ok, out = await run_pip_upgrade("server")
    assert not ok
    assert captured["cmd"][-2:] == ["kenzy[server]>=3.0.0", "kenzy-ld2450"]
    # The note says what happened AND the way out — pip's dump names packages,
    # not actions.
    assert "Nothing was changed" in out and "kenzy-ld2450" in out
    assert "upgrade or remove" in out


async def test_an_ordinary_failure_is_not_blamed_on_addons(
    tmp_path: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    monkeypatch.setattr("kenzy.plugins.installed_plugin_dists", lambda: ["kenzy-ld2450"])

    async def _fake_exec(*cmd: str, **kw: Any) -> _FakeProc:
        return _FakeProc(1, b"ERROR: No matching distribution found for kenzy==9.9.9")

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)
    ok, out = await run_pip_upgrade("server", "9.9.9")
    assert not ok and "Nothing was changed" not in out


def test_joint_failure_note_only_fires_on_resolution_conflicts() -> None:
    assert "Nothing was changed" in joint_failure_note("ResolutionImpossible", ["kenzy-x"])
    assert (
        "Nothing was changed"
        in joint_failure_note("error: conflicting dependencies kenzy…", ["kenzy-x"])
    )
    # No plugins in the set → the conflict is not an add-on story.
    assert joint_failure_note("ResolutionImpossible", []) == "ResolutionImpossible"
    assert joint_failure_note("network unreachable", ["kenzy-x"]) == "network unreachable"


# ---------------------------------------------------------------------------
# kenzy-deploy asks the REMOTE venv what it carries
# ---------------------------------------------------------------------------


class _R:
    def __init__(self, rc: int, out: str) -> None:
        self.returncode = rc
        self.stdout = out
        self.stderr = ""


def test_remote_enumeration_filters_and_tolerates_old_hosts(monkeypatch: Any) -> None:
    host = _host()
    # A well-behaved 5.1 host, plus junk that must never reach a shell command.
    monkeypatch.setattr(
        "kenzy.deploy.deploy._ssh",
        lambda h, cmd, **kw: _R(0, "kenzy-ld2450\nbad name; rm -rf /\n\n"),
    )
    assert _remote_plugin_dists(host) == ("kenzy-ld2450",)
    # An older remote has no kenzy.plugins module: the enumeration fails and
    # the sweep behaves exactly as it did before 5.1.
    monkeypatch.setattr(
        "kenzy.deploy.deploy._ssh",
        lambda h, cmd, **kw: _R(1, "ModuleNotFoundError: No module named 'kenzy.plugins'"),
    )
    assert _remote_plugin_dists(host) == ()
