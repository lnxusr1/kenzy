"""kenzy-deploy honors the constraints file the same way the per-user install does:
auto-detected (or explicit) constraints.txt is passed with -c in both install modes."""

from __future__ import annotations

from kenzy.deploy.deploy import HostConfig, _pip_target, _resolve_constraints


def _host(**kw) -> HostConfig:
    base = dict(
        name="h",
        address="1.2.3.4",
        ssh_user="pi",
        install_path="/opt/kenzy",
        venv_path="/opt/kenzy/.venv",
        python_bin="python3",
    )
    base.update(kw)
    return HostConfig(**base)


def test_pip_target_pypi_with_constraints():
    t = _pip_target(_host(install_mode="pypi"), "server", upgrade=False, constraints="/c.txt")
    assert "-c '/c.txt'" in t and "kenzy[server]" in t


def test_pip_target_pypi_upgrade_has_u_and_constraints():
    t = _pip_target(_host(install_mode="pypi"), "server", upgrade=True, constraints="/c.txt")
    assert "-U" in t and "-c '/c.txt'" in t


def test_pip_target_source_with_constraints():
    t = _pip_target(_host(install_mode="source"), "node", upgrade=False, constraints="/c.txt")
    assert "-c '/c.txt'" in t and "-e '/opt/kenzy[node]'" in t


def test_pip_target_no_constraints_has_no_c_flag():
    t = _pip_target(_host(install_mode="pypi"), "server", upgrade=False)
    assert "-c" not in t


def test_resolve_constraints_autodetect(tmp_path):
    (tmp_path / "constraints.txt").write_text("x==1\n")
    assert _resolve_constraints(_host(), tmp_path) == tmp_path / "constraints.txt"


def test_resolve_constraints_explicit_path(tmp_path):
    (tmp_path / "pins.txt").write_text("x==1\n")
    assert _resolve_constraints(_host(constraints="pins.txt"), tmp_path) == tmp_path / "pins.txt"


def test_resolve_constraints_none_when_absent(tmp_path):
    assert _resolve_constraints(_host(), tmp_path) is None
