"""The follow-up mode voice switch (v6.0): one skill, both pipelines.

The skill queues the ``set_s2s`` server action; the server applies it live
(next wake) and persists it into server.local.yaml so a restart or upgrade
cannot silently re-enable a feature someone turned off by voice.
"""

from __future__ import annotations

import pytest
import yaml

import kenzy.llm.skills as sk
from kenzy.llm.builtin_skills.followup_control import set_followup_mode
from kenzy.server.server import TranscribingServer


@pytest.fixture(autouse=True)
def _fresh_request_context():
    """Token-reset the contextvars — a bare .set() leaks into later test
    files (the tests/conftest.py trap)."""
    t_req = sk._request_ctx.set({})
    t_act = sk._actions.set([])
    yield
    sk._actions.reset(t_act)
    sk._request_ctx.reset(t_req)


# --- the skill ---------------------------------------------------------------


async def test_enabling_needs_no_confirmation():
    sk.begin_actions()
    reply = await set_followup_mode(True)
    assert "enabled" in reply
    assert sk.take_actions() == [{"type": "set_s2s", "enabled": True}]


async def test_disabling_confirms_first():
    """First call returns the question and queues NOTHING; only the confirmed
    call acts. A settings change inferred from one breath is the 5.0.6
    stop-vs-disable lesson."""
    sk.begin_actions()
    reply = await set_followup_mode(False)
    assert "CONFIRMATION REQUIRED" in reply
    assert sk.take_actions() == []

    sk.begin_actions()
    reply = await set_followup_mode(False, confirm=True)
    assert "disabled" in reply
    assert sk.take_actions() == [{"type": "set_s2s", "enabled": False}]


def test_the_switch_is_gated_to_a_recognized_voice():
    assert sk._MIN_TIER.get("set_followup_mode") == "recognized"


# --- the server side ---------------------------------------------------------


def test_disabling_applies_live_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    cfg_path = tmp_path / "server.yaml"
    cfg_path.write_text("host: 127.0.0.1\n")

    s = TranscribingServer({"s2s": {"enabled": True}})
    s._config_path = str(cfg_path)
    assert s._s2s_enabled is True

    assert s.set_s2s_enabled(False) is True
    assert s._s2s_enabled is False  # live: the bridge reads this per capture

    override = yaml.safe_load((tmp_path / "server.local.yaml").read_text())
    assert override["s2s"]["enabled"] is False  # survives a restart


def test_persisting_keeps_other_override_keys(tmp_path, monkeypatch):
    """The switch must not clobber settings the dashboard wrote."""
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    cfg_path = tmp_path / "server.yaml"
    cfg_path.write_text("host: 127.0.0.1\n")
    (tmp_path / "server.local.yaml").write_text(
        yaml.safe_dump({"dashboard": {"controls": True}, "s2s": {"hard_cap_s": 120}})
    )

    s = TranscribingServer({})
    s._config_path = str(cfg_path)
    s.set_s2s_enabled(True)

    override = yaml.safe_load((tmp_path / "server.local.yaml").read_text())
    assert override["dashboard"]["controls"] is True
    assert override["s2s"]["hard_cap_s"] == 120  # sibling key intact
    assert override["s2s"]["enabled"] is True


async def test_dispatch_reaches_the_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)
    cfg_path = tmp_path / "server.yaml"
    cfg_path.write_text("host: 127.0.0.1\n")
    srv = TranscribingServer({"s2s": {"enabled": True}})
    srv._config_path = str(cfg_path)

    await srv._dispatch_actions([{"type": "set_s2s", "enabled": False}], "k", "kitchen")
    assert srv._s2s_enabled is False
    override = yaml.safe_load((tmp_path / "server.local.yaml").read_text())
    assert override["s2s"]["enabled"] is False
