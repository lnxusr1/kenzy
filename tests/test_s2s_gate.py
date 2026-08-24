"""Gate tests — each of the five responsibilities pinned, fail-closed paths
first (spec: kenzy-design/app/s2s-design.md, "The gate, precisely")."""

from __future__ import annotations

from kenzy.s2s.engine import ToolCall
from kenzy.s2s.gate import AuditRecord, Speaker, ToolRule, TurnGate


def _call(name: str = "set_light") -> ToolCall:
    return ToolCall(call_id="c1", name=name, arguments_json="{}")


class _Harness:
    def __init__(self, tier: str = "recognized", hold_audio: bool = False,
                 screen_hit: str | None = None) -> None:
        self.speaker = Speaker("Alex", tier)
        self.records: list[AuditRecord] = []
        self.gate = TurnGate(
            "t1",
            identity=lambda: self.speaker,
            tool_rules={
                "set_light": ToolRule(min_tier="recognized"),
                "check_mail": ToolRule(min_tier="recognized", detach=True),
                "get_time": ToolRule(min_tier="unknown"),
            },
            audit=self.records.append,
            secret_screen=(lambda _text: screen_hit),
            hold_audio=hold_audio,
        )

    def events(self) -> list[str]:
        return [r.event for r in self.records]


def test_ordering_tool_call_held_until_transcript() -> None:
    h = _Harness()
    assert h.gate.on_tool_call(_call()) is None  # held: no transcript on record
    verdicts = h.gate.on_transcript("turn on the office light")
    assert len(verdicts) == 1 and verdicts[0].allowed
    # the record shows transcript BEFORE the verdict — the invariant, audited
    assert h.events() == ["tool_held", "transcript", "tool_allowed"]
    assert h.records[-1].transcript == "turn on the office light"


def test_audio_held_and_drained_for_speculating_engines() -> None:
    h = _Harness(hold_audio=True)
    assert h.gate.on_audio(b"\x01\x01") == b""  # held pre-clearance
    h.gate.on_transcript("hello")
    assert h.gate.drain_audio() == b"\x01\x01"
    assert h.gate.on_audio(b"\x02\x02") == b"\x02\x02"  # pass-through once cleared


def test_audio_passes_through_on_cascade_profiles() -> None:
    h = _Harness(hold_audio=False)
    assert h.gate.on_audio(b"\x03\x03") == b"\x03\x03"  # cleared-by-construction engines


def test_tier_gating_fails_closed() -> None:
    h = _Harness(tier="unknown")
    h.gate.on_transcript("turn on the light")
    denied = h.gate.on_tool_call(_call())
    assert denied is not None and not denied.allowed and "tier" in denied.reason
    allowed = h.gate.on_tool_call(_call("get_time"))  # min_tier unknown: fine
    assert allowed is not None and allowed.allowed


def test_unknown_tool_denied() -> None:
    h = _Harness()
    h.gate.on_transcript("do the thing")
    verdict = h.gate.on_tool_call(_call("not_a_tool"))
    assert verdict is not None and not verdict.allowed and "unknown tool" in verdict.reason


def test_secret_diversion_fails_actions_closed() -> None:
    h = _Harness(screen_hit="explicit secret matched")
    h.gate.on_transcript("the garage code is 1234")
    assert h.gate.diversion is not None
    verdict = h.gate.on_tool_call(_call())
    assert verdict is not None and not verdict.allowed
    assert "secret" in verdict.reason
    assert "secret_diverted" in h.events()


def test_detach_surfaces_on_allowed_verdict() -> None:
    h = _Harness()
    h.gate.on_transcript("check my mail")
    verdict = h.gate.on_tool_call(_call("check_mail"))
    assert verdict is not None and verdict.allowed and verdict.detach  # Layer C hand-off


def test_identity_read_at_action_time_not_snapshot() -> None:
    h = _Harness(tier="unknown")
    assert h.gate.on_tool_call(_call()) is None  # held pre-transcript
    h.speaker = Speaker("Alex", "recognized")  # segment-wise ID refined mid-turn
    verdicts = h.gate.on_transcript("turn on the office light")
    assert verdicts[0].allowed  # decision 6: the verdict used the CURRENT tier
    assert h.records[-1].tier == "recognized"
