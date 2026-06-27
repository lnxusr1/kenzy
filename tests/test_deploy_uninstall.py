"""kenzy-deploy uninstall: the rm -rf path guard refuses dangerously shallow or
critical directories, so a misconfigured install_path/venv_path can't wipe a system
location (e.g. /, /opt, $empty), while real install dirs are allowed."""

from __future__ import annotations

import pytest

from kenzy.deploy.deploy import _safe_to_remove


@pytest.mark.parametrize(
    "path",
    [
        "/opt/kenzy",
        "/opt/kenzy/.venv",
        "/usr/local/kenzy",
        "/home/pi/kenzy",
    ],
)
def test_safe_paths_allowed(path: str) -> None:
    assert _safe_to_remove(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/",
        "/opt",
        "/opt/",  # trailing slash must not change the depth check
        "/home",
        "/usr",
        "/etc",
        "/var",
    ],
)
def test_unsafe_paths_refused(path: str) -> None:
    assert _safe_to_remove(path) is False
