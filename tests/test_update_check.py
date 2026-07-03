"""Dashboard update-check (visibility layer for the upgrade feature): version
comparison and the /api/upgrade state (PyPI fetch mocked)."""

from __future__ import annotations

from kenzy.server.dashboard import Dashboard, DashboardConfig, _is_newer, _version_tuple
from kenzy.server.server import AudioServer


def test_version_tuple():
    assert _version_tuple("3.1.10") == (3, 1, 10)
    assert _version_tuple("3.1") == (3, 1)
    assert _version_tuple("dev") == (0,)  # non-numeric → 0


def test_is_newer():
    assert _is_newer("3.1.2", "3.1.1") is True
    assert _is_newer("3.1.10", "3.1.9") is True  # numeric, not lexical
    assert _is_newer("3.2.0", "3.1.9") is True
    assert _is_newer("3.1.1", "3.1.1") is False
    assert _is_newer("3.1.0", "3.1.1") is False


def _dash() -> Dashboard:
    return Dashboard(AudioServer({}), {}, DashboardConfig(enabled=True))


async def test_upgrade_state_update_available(monkeypatch):
    import kenzy.server.dashboard as d

    monkeypatch.setattr(d, "kenzy_version", lambda: "3.1.0")
    dash = _dash()

    async def fake_latest():
        return "3.1.2"

    monkeypatch.setattr(dash, "_latest_pypi_version", fake_latest)
    state = await dash._upgrade_state()
    assert state == {
        "current": "3.1.0",
        "latest": "3.1.2",
        "update_available": True,
        "checkable": True,
        "controls": True,  # defaults on when the key is absent
    }


async def test_upgrade_state_up_to_date(monkeypatch):
    import kenzy.server.dashboard as d

    monkeypatch.setattr(d, "kenzy_version", lambda: "3.1.2")
    dash = _dash()

    async def fake_latest():
        return "3.1.2"

    monkeypatch.setattr(dash, "_latest_pypi_version", fake_latest)
    assert (await dash._upgrade_state())["update_available"] is False


async def test_upgrade_state_offline(monkeypatch):
    dash = _dash()

    async def no_pypi():
        return None

    monkeypatch.setattr(dash, "_latest_pypi_version", no_pypi)
    state = await dash._upgrade_state()
    assert state["checkable"] is False and state["update_available"] is False


async def test_upgrade_state_dev_build_never_flags(monkeypatch):
    import kenzy.server.dashboard as d

    monkeypatch.setattr(d, "kenzy_version", lambda: "dev")
    dash = _dash()

    async def fake_latest():
        return "3.1.2"

    monkeypatch.setattr(dash, "_latest_pypi_version", fake_latest)
    assert (await dash._upgrade_state())["update_available"] is False
