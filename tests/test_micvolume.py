"""mic_volume: the managed ALSA capture gain.

The module's promise is that every failure path EXPLAINS itself — a missing
binary, an unmappable device, a card with no capture control — because the
5.0.4 lesson is that a managed setting silently not managing anything looks
identical to one that is. Driven through an injected runner: these tests pin
the amixer conversation, not the presence of sound hardware on the CI box.
"""

from __future__ import annotations

import sys

import pytest

from kenzy.node.client import _parse_mic_volume
from kenzy.node.micvolume import (
    card_from_device_name,
    pick_capture_control,
    set_capture_volume,
)

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="mic_volume is Linux/ALSA-only"
)

SCONTROLS = "Simple mixer control 'Speaker',0\nSimple mixer control 'Mic',0\n"
SGET_PLAYBACK = "Capabilities: pvolume pswitch\n"
SGET_CAPTURE = "Capabilities: cvolume cswitch\n"


class _Amixer:
    """A scripted amixer: records every invocation, answers per subcommand."""

    def __init__(self, answers: dict[str, tuple[int, str]]):
        self.calls: list[list[str]] = []
        self._answers = answers

    def __call__(self, cmd: list[str]) -> tuple[int, str]:
        self.calls.append(cmd)
        sub = cmd[3] if len(cmd) > 3 else ""
        key = f"{sub}:{cmd[4]}" if sub in ("sget", "sset") else sub
        return self._answers.get(key, (1, f"no answer scripted for {key}"))


def test_card_parses_from_portaudio_names() -> None:
    assert card_from_device_name("Anker PowerConf S330: USB Audio (hw:2,0)") == 2
    assert card_from_device_name("SP300U: USB Audio (hw:11,0)") == 11
    assert card_from_device_name("pulse") is None
    assert card_from_device_name("") is None


def test_the_happy_path_picks_the_capture_control_and_sets_it() -> None:
    run = _Amixer(
        {
            "scontrols": (0, SCONTROLS),
            "sget:Speaker": (0, SGET_PLAYBACK),  # playback-only: skipped
            "sget:Mic": (0, SGET_CAPTURE),
            "sset:Mic": (0, "ok"),
        }
    )
    status = set_capture_volume("Anker PowerConf S330: USB Audio (hw:2,0)", 40, run)
    assert status["applied"] is True
    assert status["control"] == "Mic" and status["card"] == 2
    assert "'Mic' on card 2 → 40%" in status["detail"]
    # The trailing `capture` selector: on combined controls (the S330: one
    # simple control with pvolume AND cvolume) it keeps the write off the
    # playback side — Kenzy's canonical playback volume must stay the only
    # writer there.
    assert ["amixer", "-c", "2", "sset", "Mic", "40%", "capture"] in run.calls


def test_percent_is_clamped_before_it_reaches_the_mixer() -> None:
    run = _Amixer(
        {
            "scontrols": (0, "Simple mixer control 'Capture',0\n"),
            "sget:Capture": (0, SGET_CAPTURE),
            "sset:Capture": (0, "ok"),
        }
    )
    set_capture_volume("X (hw:0,0)", 250, run)
    assert ["amixer", "-c", "0", "sset", "Capture", "100%", "capture"] in run.calls


def test_every_refusal_names_its_reason() -> None:
    # A device the mixer can't be reached through (Pulse alias, no hw:N).
    status = set_capture_volume("pulse", 50, _Amixer({}))
    assert not status["applied"] and "can't map audio device 'pulse'" in status["detail"]

    # amixer missing entirely — the fix is named.
    status = set_capture_volume("X (hw:1,0)", 50, _Amixer({"scontrols": (127, "not found")}))
    assert not status["applied"] and "alsa-utils" in status["detail"]

    # A card with only playback controls — what WAS found is listed.
    run = _Amixer(
        {"scontrols": (0, "Simple mixer control 'Speaker',0\n"), "sget:Speaker": (0, SGET_PLAYBACK)}
    )
    status = set_capture_volume("X (hw:1,0)", 50, run)
    assert not status["applied"]
    assert "no capture-volume control on card 1" in status["detail"]
    assert "Speaker" in status["detail"]

    # The set itself failing surfaces amixer's own words.
    run = _Amixer(
        {
            "scontrols": (0, "Simple mixer control 'Mic',0\n"),
            "sget:Mic": (0, SGET_CAPTURE),
            "sset:Mic": (1, "Invalid argument"),
        }
    )
    status = set_capture_volume("X (hw:1,0)", 50, run)
    assert not status["applied"] and "Invalid argument" in status["detail"]


def test_pick_prefers_the_first_capture_capable_control() -> None:
    run = _Amixer(
        {
            "scontrols": (0, SCONTROLS),
            "sget:Speaker": (0, SGET_PLAYBACK),
            "sget:Mic": (0, SGET_CAPTURE),
        }
    )
    control, names = pick_capture_control(2, run)
    assert control == "Mic" and names == ["Speaker", "Mic"]


def test_config_parsing_is_defensive() -> None:
    """A typo in one key must never cost the whole config apply: garbage maps
    to None (unmanaged), values clamp, unset stays unset."""
    assert _parse_mic_volume(None) is None
    assert _parse_mic_volume("") is None
    assert _parse_mic_volume(40) == 40
    assert _parse_mic_volume("40") == 40  # the grid saves strings
    assert _parse_mic_volume(250) == 100
    assert _parse_mic_volume(-5) == 0
    assert _parse_mic_volume("forty") is None
    assert _parse_mic_volume(0) == 0  # 0 is a value, not an unset
