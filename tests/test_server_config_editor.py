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


def _dash(tmp_path, cfg: dict) -> Dashboard:
    cfgfile = tmp_path / "server.yaml"
    _write(cfgfile, cfg)
    server = AudioServer(cfg)
    return Dashboard(server, cfg, DashboardConfig.from_cfg(cfg), config_path=cfgfile)


def test_write_override_validates_and_persists(tmp_path):
    dash = _dash(tmp_path, {"dashboard": {"enabled": True}})
    dash._write_server_override(
        {"dashboard.logs": True, "stt.timeout": "45", "speaker.unknown_speaker": "guest"}
    )
    written = yaml.safe_load((tmp_path / "server.local.yaml").read_text())
    assert written["dashboard"]["logs"] is True
    assert written["stt"]["timeout"] == 45.0  # coerced to num
    assert written["speaker"]["unknown_speaker"] == "guest"


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


def test_server_config_state_reports_effective_and_overridden(tmp_path):
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
    assert by_key["stt.url"]["overridden"] is False
    assert by_key["stt.url"]["value"] == "http://a/transcribe"
    # An unset editable key surfaces as value None (not an error).
    assert by_key["llm.url"]["value"] is None


def test_server_config_state_not_writable_without_path():
    server = AudioServer({})
    dash = Dashboard(server, {}, DashboardConfig(), config_path=None)
    assert dash._server_config_state()["writable"] is False
    with pytest.raises(ValueError, match="location is unknown"):
        dash._write_server_override({"dashboard.logs": True})
