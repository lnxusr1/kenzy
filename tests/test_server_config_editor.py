"""Tests for the scoped server self-config editor: the server.local.yaml override
layer (load_server_config), and the dashboard's read/validate/write helpers."""

from __future__ import annotations

import pytest
import yaml

from kenzy.server.dashboard import Dashboard, DashboardConfig
from kenzy.server.server import (
    AudioServer,
    _server_override_path,
    load_server_config,
)


def _write(path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data))


def test_override_path_is_beside_server_yaml(tmp_path):
    cfg = tmp_path / "server.yaml"
    assert _server_override_path(cfg) == tmp_path / "server.local.yaml"


def test_load_server_config_deep_merges_override(tmp_path):
    cfg = tmp_path / "server.yaml"
    _write(
        cfg,
        {
            "host": "0.0.0.0",
            "dashboard": {"enabled": True, "logs": False, "controls": False},
            "stt": {"url": "http://a/transcribe", "timeout": 60.0},
        },
    )
    _write(
        tmp_path / "server.local.yaml",
        {"dashboard": {"logs": True}, "stt": {"timeout": 30.0}},
    )
    merged = load_server_config(cfg)
    # Override wins per-key; siblings under the same branch are preserved.
    assert merged["dashboard"] == {"enabled": True, "logs": True, "controls": False}
    assert merged["stt"] == {"url": "http://a/transcribe", "timeout": 30.0}
    assert merged["host"] == "0.0.0.0"


def test_load_server_config_no_override(tmp_path):
    cfg = tmp_path / "server.yaml"
    _write(cfg, {"host": "0.0.0.0", "port": 8765})
    assert load_server_config(cfg) == {"host": "0.0.0.0", "port": 8765}


def test_override_never_lives_in_the_packaged_data_dir(tmp_path, monkeypatch):
    """A server running off the packaged default config must read/write its
    override in the config home — an override baked into the wheel (the 3.7.1
    `experimental: true` accident) must be ignored, and dashboard edits must
    not land inside site-packages."""
    import kenzy.config as kconfig

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    home = tmp_path / "home"
    (home / "configs").mkdir(parents=True)
    monkeypatch.setattr(kconfig, "_PACKAGED_CONFIGS", pkg)
    monkeypatch.setenv("KENZY_HOME", str(home))

    _write(pkg / "server.yaml", {"host": "0.0.0.0"})
    _write(pkg / "server.local.yaml", {"experimental": True})  # wheel-baked poison

    # Redirected to the config home; the packaged copy is never read.
    assert _server_override_path(pkg / "server.yaml") == home / "configs" / "server.local.yaml"
    assert "experimental" not in load_server_config(pkg / "server.yaml")

    # A config-home override still applies to a packaged-default server.
    _write(home / "configs" / "server.local.yaml", {"experimental": True})
    assert load_server_config(pkg / "server.yaml")["experimental"] is True


def _dash(tmp_path, cfg: dict) -> Dashboard:
    cfgfile = tmp_path / "server.yaml"
    _write(cfgfile, cfg)
    server = AudioServer(cfg)
    return Dashboard(server, cfg, DashboardConfig.from_cfg(cfg), config_path=cfgfile)


def test_write_override_validates_and_persists(tmp_path):
    dash = _dash(tmp_path, {"dashboard": {"enabled": True}})
    dash._write_server_override(
        {"dashboard.logs": True, "stt.timeout": "45", "discovery.instance": "my-server"}
    )
    written = yaml.safe_load((tmp_path / "server.local.yaml").read_text())
    assert written["dashboard"]["logs"] is True
    assert written["stt"]["timeout"] == 45.0  # coerced to num
    assert written["discovery"]["instance"] == "my-server"


def test_write_override_rejects_unknown_keys(tmp_path):
    dash = _dash(tmp_path, {})
    with pytest.raises(ValueError, match="unsupported keys"):
        dash._write_server_override({"dashboard.bind": "0.0.0.0"})
    with pytest.raises(ValueError, match="unsupported keys"):
        dash._write_server_override({"discovery.token": "leak"})
    # nothing should have been written
    assert not (tmp_path / "server.local.yaml").exists()


def test_write_override_rejects_bad_number(tmp_path):
    dash = _dash(tmp_path, {})
    with pytest.raises(ValueError, match="invalid value for stt.timeout"):
        dash._write_server_override({"stt.timeout": "not-a-number"})


def test_server_config_state_separates_override_from_inherited(tmp_path):
    """Node-editor contract: `value` = the override layer only (None when the key
    isn't in server.local.yaml); `inherited` = the server.yaml value that applies
    when unset (the UI's placeholder)."""
    cfg = {
        "dashboard": {"enabled": True, "logs": False},
        "stt": {"url": "http://a/transcribe", "timeout": 60.0},
    }
    dash = _dash(tmp_path, cfg)
    _write(tmp_path / "server.local.yaml", {"dashboard": {"logs": True}})
    state = dash._server_config_state()
    assert state["writable"] is True
    by_key = {f["key"]: f for f in state["fields"]}
    assert by_key["dashboard.logs"]["overridden"] is True
    assert by_key["dashboard.logs"]["value"] is True
    assert by_key["dashboard.logs"]["inherited"] is False  # server.yaml's value
    # A key set only in server.yaml: not overridden, value empty, inherited filled.
    assert by_key["stt.url"]["overridden"] is False
    assert by_key["stt.url"]["value"] is None
    assert by_key["stt.url"]["inherited"] == "http://a/transcribe"
    # An entirely unset editable key surfaces as None/None (not an error).
    assert by_key["llm.url"]["value"] is None
    assert by_key["llm.url"]["inherited"] is None


def test_write_override_null_unsets_key(tmp_path):
    """A null patch value removes the key from server.local.yaml (revert to
    inherited), pruning empty parents; the last key removes the file."""
    dash = _dash(tmp_path, {"dashboard": {"enabled": True}})
    dash._write_server_override({"integrations.mqtt.host": "10.0.0.9", "dashboard.logs": False})
    dash._write_server_override({"integrations.mqtt.host": None})
    written = yaml.safe_load((tmp_path / "server.local.yaml").read_text())
    assert "integrations" not in written  # empty parents pruned
    assert written == {"dashboard": {"logs": False}}
    dash._write_server_override({"dashboard.logs": None})
    assert not (tmp_path / "server.local.yaml").exists()  # empty override = no file


def test_server_config_state_not_writable_without_path():
    server = AudioServer({})
    dash = Dashboard(server, {}, DashboardConfig(), config_path=None)
    assert dash._server_config_state()["writable"] is False
    with pytest.raises(ValueError, match="location is unknown"):
        dash._write_server_override({"dashboard.logs": True})
