"""The spoken off-switch (5.0.6) — the panic button.

It must work when the language model is a suspect, so it's a fast intent and
these tests exercise the matcher directly.
"""

from __future__ import annotations

import yaml

from kenzy.llm.builtin_skills.proactive_control import classify
from kenzy.server.proactive import ProactiveGate
from kenzy.server.server import TranscribingServer


def test_making_the_noise_stop_is_not_a_disable():
    """The phrase you shout at a blaring smoke alarm must NOT turn off every
    future safety announcement. That was the first draft, and it failed
    silently: the session opening already quieted the alarm, so it did what you
    wanted in the moment and the permanent half went unnoticed."""
    for text in (
        "stop the alerts",
        "stop the alert",
        "turn off the alerts",
        "shut off that alarm",
        "silence the alerts",
        "cancel the alert",
        "quiet the announcements",
    ):
        assert classify(text) == "silence", text


def test_disabling_needs_a_deliberate_word():
    for text in (
        "disable the alerts",
        "disable alerts",
        "deactivate the announcements",
        # ...or an explicit "and I mean it" on the casual phrasing.
        "stop the alerts permanently",
        "turn off the alerts for good",
        "stop the announcements entirely",
    ):
        assert classify(text) == "disable", text


def test_permanently_beats_the_silence_pattern():
    """"stop the alerts permanently" contains "stop the alerts" — the more
    specific intent has to win, or the qualifier is silently ignored."""
    assert classify("stop the alerts permanently") == "disable"


def test_turning_them_back_on():
    for text in (
        "turn on the alerts",
        "enable announcements",
        "re-enable alerts",
        "resume notifications",
        "start the alerts",
    ):
        assert classify(text) == "enable", text


def test_unrelated_speech_misses():
    for text in (
        "",
        "what time is it",
        "turn off the kitchen lights",
        "stop the timer",
        "turn on the lamp",
    ):
        assert classify(text) is None, text


def test_silencing_is_open_to_any_voice_but_disabling_is_not():
    """Whoever is standing in front of a shrieking speaker gets to stop it.
    Turning the feature off is a settings change and needs a known voice."""
    from kenzy.llm import skills as sk

    assert sk._MIN_TIER.get("fast_proactive_silence") is None
    assert sk._MIN_TIER.get("fast_proactive_control") == "recognized"


# --- the server side ---------------------------------------------------------


def test_disabling_applies_live_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    cfg_path = tmp_path / "server.yaml"
    cfg_path.write_text("host: 127.0.0.1\n")

    s = TranscribingServer({})
    s._config_path = str(cfg_path)
    s._proactive = ProactiveGate.from_config({"enabled": True, "safety": {"enabled": True}})

    assert s.set_proactive_enabled(False) is True
    assert s._proactive.enabled is False  # live

    override = yaml.safe_load((tmp_path / "server.local.yaml").read_text())
    assert override["proactive"]["enabled"] is False  # survives a restart


def test_re_enabling_writes_back(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    cfg_path = tmp_path / "server.yaml"
    cfg_path.write_text("host: 127.0.0.1\n")
    s = TranscribingServer({})
    s._config_path = str(cfg_path)
    s._proactive = ProactiveGate.from_config({"enabled": True})

    s.set_proactive_enabled(False)
    s.set_proactive_enabled(True)
    override = yaml.safe_load((tmp_path / "server.local.yaml").read_text())
    assert override["proactive"]["enabled"] is True


def test_persisting_keeps_other_override_keys(tmp_path, monkeypatch):
    """The off-switch must not clobber settings the dashboard wrote."""
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    cfg_path = tmp_path / "server.yaml"
    cfg_path.write_text("host: 127.0.0.1\n")
    (tmp_path / "server.local.yaml").write_text(
        yaml.safe_dump({"dashboard": {"controls": True}, "proactive": {"rate_limit": 3}})
    )

    s = TranscribingServer({})
    s._config_path = str(cfg_path)
    s._proactive = ProactiveGate.from_config({"enabled": True})
    s.set_proactive_enabled(False)

    override = yaml.safe_load((tmp_path / "server.local.yaml").read_text())
    assert override["dashboard"]["controls"] is True
    assert override["proactive"]["rate_limit"] == 3  # sibling key intact
    assert override["proactive"]["enabled"] is False


def test_a_server_with_no_gate_reports_failure_rather_than_lying(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = TranscribingServer({})
    assert s.set_proactive_enabled(False) is False


# --- the confirmation --------------------------------------------------------


async def _confirm_with(monkeypatch, answer):
    """Drive the disable intent with a canned answer to the confirmation."""
    from kenzy.llm import skills as sk
    from kenzy.llm.builtin_skills import proactive_control as pc

    asked = []

    async def fake_ask(prompt, *a, **kw):  # noqa: ANN001
        asked.append(prompt)
        return answer

    monkeypatch.setattr(pc, "ask", fake_ask)
    actions: list[dict] = []
    monkeypatch.setattr(pc, "add_action", actions.append)
    fn = next(f for _, name, f in sk._FAST_REGISTRY if name == "fast_proactive_control")
    res = await fn("disable the alerts", "office", "Alex")
    return res, actions, asked


async def test_disabling_asks_before_it_does_it(monkeypatch):
    res, actions, asked = await _confirm_with(monkeypatch, "yes")
    assert asked and "permanently disable" in asked[0]
    assert "smoke" in asked[0]  # says what it actually costs
    assert actions == [{"type": "set_proactive", "enabled": False}]
    assert "disabled" in res.text.lower()


async def test_declining_leaves_the_alerts_on(monkeypatch):
    res, actions, _ = await _confirm_with(monkeypatch, "no")
    assert actions == []
    assert "leave the alerts on" in res.text.lower()


async def test_an_ambiguous_answer_never_disables(monkeypatch):
    """Guessing wrong here is silent and lasts until somebody notices months
    later, so anything that isn't a plain yes leaves them armed."""
    for answer in ("maybe", "yes but not the smoke one", "i guess", "hmm", ""):
        _, actions, _ = await _confirm_with(monkeypatch, answer)
        assert actions == [], answer


async def test_a_timed_out_confirmation_never_disables(monkeypatch):
    """ask() returns None when the window expires with no speech."""
    _, actions, _ = await _confirm_with(monkeypatch, None)
    assert actions == []


def test_the_urgent_path_has_no_confirmation():
    """Friction on disable is only affordable because silencing has none —
    nobody should answer a question to make a shrieking speaker be quiet."""
    import inspect

    from kenzy.llm.builtin_skills import proactive_control as pc

    src = inspect.getsource(pc.fast_proactive_silence)
    assert "ask(" not in src
