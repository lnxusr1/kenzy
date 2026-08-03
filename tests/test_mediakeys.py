"""USB speakerphone volume buttons (5.0.4): the pure selection rules, the
volume_delta wire, and the invariant that hardware buttons ride the SAME
server-owned volume path as every other surface."""

from __future__ import annotations

import json

from kenzy import protocol
from kenzy.node.mediakeys import Candidate, select_endpoint, usb_parent_of

SPK = Candidate(
    path="/dev/input/event3",
    name="SPEAKPHONE SP300U",
    phys="usb-0000:00:14.0-3/input3",
    usb_parent="1-3",
    keys=frozenset({113, 114, 115}),
)
KEYBOARD = Candidate(
    path="/dev/input/event0",
    name="CHICONY USB Keyboard",
    phys="usb-0000:00:14.0-9/input1",
    usb_parent="1-9",  # different physical device — no audio sibling
    keys=frozenset({114, 115, 30, 48}),
)
TOUCHPAD = Candidate(
    path="/dev/input/event5", name="Touchpad", phys="", usb_parent="", keys=frozenset({272})
)


# ---------------------------------------------------------------------------
# sysfs parsing
# ---------------------------------------------------------------------------


def test_usb_parent_takes_the_device_not_the_interface():
    p = "/sys/devices/pci0000:00/0000:00:14.0/usb1/1-3/1-3:1.3/0003:4658:3220.0001/input/input3"
    assert usb_parent_of(p) == "1-3"


def test_usb_parent_handles_nested_hubs_and_non_usb():
    assert usb_parent_of("/sys/devices/.../usb3/3-1/3-1.4/3-1.4:1.0/sound/card2") == "3-1.4"
    assert usb_parent_of("/sys/devices/platform/i8042/serio1/input/input5") == ""


# ---------------------------------------------------------------------------
# Selection — auto
# ---------------------------------------------------------------------------


def test_auto_picks_the_audio_sibling_only():
    choice, why = select_endpoint([KEYBOARD, SPK, TOUCHPAD], "1-3", "auto")
    assert choice is SPK and "sibling" in why


def test_auto_never_picks_a_keyboard_on_another_device():
    # Acceptance check 3: media keys on an unrelated device must not qualify.
    choice, why = select_endpoint([KEYBOARD, TOUCHPAD], "1-3", "auto")
    assert choice is None and "no sibling" in why


def test_auto_without_audio_parent_uses_the_fallback_whitelist():
    # No resolvable audio parent and no sound-card whitelist ⇒ nothing —
    # the fallback needs positive evidence that a parent hosts audio.
    choice, why = select_endpoint([SPK], "", "auto")
    assert choice is None
    # With the whitelist supplied, the same candidate qualifies (tier 2).
    choice, _ = select_endpoint([SPK], "", "auto", sound_parents={"1-3"})
    assert choice is SPK


def test_auto_ambiguity_does_nothing_and_names_candidates():
    twin = Candidate(
        path="/dev/input/event7", name="Other HID", phys="x", usb_parent="1-3",
        keys=frozenset({115}),
    )  # fmt: skip
    choice, why = select_endpoint([SPK, twin], "1-3", "auto")
    assert choice is None
    assert "SPEAKPHONE SP300U" in why and "Other HID" in why


# ---------------------------------------------------------------------------
# Selection — explicit
# ---------------------------------------------------------------------------


def test_explicit_matches_name_or_phys_case_insensitively():
    choice, _ = select_endpoint([KEYBOARD, SPK], "", "sp300u")
    assert choice is SPK
    choice, _ = select_endpoint([KEYBOARD, SPK], "", "usb-0000:00:14.0-3/input3")
    assert choice is SPK


def test_explicit_miss_and_ambiguity_fail_closed():
    choice, why = select_endpoint([SPK], "", "anker")
    assert choice is None and "no input device" in why
    choice, why = select_endpoint([KEYBOARD, SPK], "", "usb-0000")
    assert choice is None and "ambiguous" in why


# ---------------------------------------------------------------------------
# The wire and the server side
# ---------------------------------------------------------------------------


def test_volume_delta_frame_shape():
    msg = json.loads(protocol.volume_delta(-5))
    assert msg == {"type": protocol.MSG_VOLUME_DELTA, "delta": -5}


async def test_server_applies_own_node_delta_via_canonical_path(tmp_path, monkeypatch):
    from tests.test_ask_server import _server_with_node

    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    srv, ws = _server_with_node()
    session = srv._nodes["k"]
    calls: list[tuple[str, int]] = []
    real = srv.set_node_volume

    async def spy(node_id, level=None, delta=None):  # noqa: ANN001, ANN202
        calls.append((node_id, delta))
        return await real(node_id, level=level, delta=delta)

    monkeypatch.setattr(srv, "set_node_volume", spy)
    await srv._handle_control(session, json.loads(protocol.volume_delta(-5)))
    # The frame moved THIS node, through the one canonical volume path…
    assert calls == [("k", -5)]
    # …and the result is persisted in the per-node override, like any surface.
    assert srv.read_node_override("k")["volume"] == 95


async def test_server_clamps_hostile_or_misconfigured_deltas(tmp_path, monkeypatch):
    from tests.test_ask_server import _server_with_node

    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    srv, ws = _server_with_node()
    session = srv._nodes["k"]
    await srv._handle_control(session, {"type": protocol.MSG_VOLUME_DELTA, "delta": -1000})
    assert srv.read_node_override("k")["volume"] == 80  # one press ≤ 20 points
    await srv._handle_control(session, {"type": protocol.MSG_VOLUME_DELTA, "delta": "junk"})
    assert srv.read_node_override("k")["volume"] == 80  # unparseable = ignored


# ---------------------------------------------------------------------------
# Node config plumbing
# ---------------------------------------------------------------------------


def _node(tmp_path, monkeypatch, **cfg):
    from kenzy.node.client import NodeClient

    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    return NodeClient({"node_id": "n1", "room_id": "Office", **cfg})


def test_defaults_off_and_step_clamped(tmp_path, monkeypatch):
    n = _node(tmp_path, monkeypatch)
    assert n._mk_enabled is False and n._mk_device == "auto" and n._mk_step == 5
    n2 = _node(tmp_path, monkeypatch, volume_buttons=True, volume_button_step=900)
    assert n2._mk_enabled is True and n2._mk_step == 20  # clamped, not trusted


def test_live_apply_updates_fields_without_a_loop(tmp_path, monkeypatch):
    # _apply_pulled_config runs in sync tests with no running loop — the
    # watcher sync must record and step aside, never crash.
    n = _node(tmp_path, monkeypatch)
    n._apply_pulled_config(
        {"volume_buttons": True, "volume_button_device": "SP300U", "volume_button_step": 2}
    )
    assert n._mk_enabled is True and n._mk_device == "SP300U" and n._mk_step == 2
    n._apply_pulled_config({"volume_buttons": False})
    assert n._mk_enabled is False


async def test_watcher_lifecycle_follows_config(tmp_path, monkeypatch):
    n = _node(tmp_path, monkeypatch)
    n._apply_pulled_config({"volume_buttons": True})
    assert n._mediakeys_task is not None  # started (evdev may fail inside; task exists)
    first = n._mediakeys_task
    n._apply_pulled_config({"volume_buttons": True})
    assert n._mediakeys_task is first  # unchanged config = same task, no churn
    n._apply_pulled_config({"volume_buttons": False})
    assert n._mediakeys_task is None  # disabled = cancelled
    if first.cancelled() is False:
        first.cancel()


async def test_delta_only_sent_when_registered(tmp_path, monkeypatch):
    n = _node(tmp_path, monkeypatch)

    class _WS:
        def __init__(self):
            self.sent = []

        async def send(self, m):
            self.sent.append(m)

    ws = _WS()
    n._ws = ws
    n._registered = False
    await n._send_volume_delta(5)
    assert ws.sent == []  # orphaned press dropped, never queued
    n._registered = True
    await n._send_volume_delta(5)
    assert json.loads(ws.sent[0])["type"] == protocol.MSG_VOLUME_DELTA


# ---------------------------------------------------------------------------
# Hold-to-repeat (typematic) — the SP300U sends NO autorepeat (no EV_REP), so
# the node synthesizes it. Driven through _on_key_event, no hardware needed.
# ---------------------------------------------------------------------------


def _watcher(sent):
    from kenzy.node import mediakeys as mk

    async def send(delta):
        sent.append(delta)

    return mk.MediaKeyWatcher(
        step=5, device_match="auto", audio_device=None, send_delta=send, on_status=lambda s: None
    )


async def test_tap_sends_exactly_one_step(monkeypatch):
    from kenzy.node import mediakeys as mk

    monkeypatch.setattr(mk, "_REPEAT_DELAY_S", 0.08)
    monkeypatch.setattr(mk, "_REPEAT_PERIOD_S", 0.02)
    monkeypatch.setattr(mk, "_MIN_SEND_INTERVAL_S", 0.0)
    sent: list[int] = []
    w = _watcher(sent)
    await w._on_key_event(mk.KEY_VOLUMEUP, 1)
    await w._on_key_event(mk.KEY_VOLUMEUP, 0)  # released before the grace delay
    import asyncio

    await asyncio.sleep(0.15)
    assert sent == [5]  # a normal tap never double-steps


async def test_hold_repeats_until_release(monkeypatch):
    from kenzy.node import mediakeys as mk

    monkeypatch.setattr(mk, "_REPEAT_DELAY_S", 0.03)
    monkeypatch.setattr(mk, "_REPEAT_PERIOD_S", 0.02)
    monkeypatch.setattr(mk, "_MIN_SEND_INTERVAL_S", 0.0)
    sent: list[int] = []
    w = _watcher(sent)
    import asyncio

    await w._on_key_event(mk.KEY_VOLUMEDOWN, 1)
    await asyncio.sleep(0.12)  # held: grace then steady steps
    await w._on_key_event(mk.KEY_VOLUMEDOWN, 0)
    n = len(sent)
    assert n >= 3 and set(sent) == {-5}
    await asyncio.sleep(0.06)
    assert len(sent) == n  # release stopped it


async def test_device_native_repeats_stand_down_the_synthesizer(monkeypatch):
    from kenzy.node import mediakeys as mk

    monkeypatch.setattr(mk, "_REPEAT_DELAY_S", 0.03)
    monkeypatch.setattr(mk, "_REPEAT_PERIOD_S", 0.02)
    monkeypatch.setattr(mk, "_MIN_SEND_INTERVAL_S", 0.0)
    sent: list[int] = []
    w = _watcher(sent)
    import asyncio

    await w._on_key_event(mk.KEY_VOLUMEUP, 1)
    await w._on_key_event(mk.KEY_VOLUMEUP, 2)  # the device itself repeats…
    assert w._repeat_task is None  # …so the synthesizer stands down
    await asyncio.sleep(0.08)
    assert len(sent) == 2  # no synthetic extras on top of device repeats


async def test_override_writes_poke_node_config_listeners(tmp_path, monkeypatch):
    """The dashboard-staleness fix: ANY override write (a button press through
    set_node_volume included) fires the node-config listeners, so an open
    config page hears about volume changes it didn't make."""
    from tests.test_ask_server import _server_with_node

    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    srv, ws = _server_with_node()
    poked: list[str] = []
    srv.add_node_config_listener(poked.append)
    await srv._handle_control(srv._nodes["k"], json.loads(protocol.volume_delta(5)))
    assert poked == ["k"]


async def test_editor_save_preserves_watchdog_but_edits_media_keys(tmp_path, monkeypatch):
    """`watchdog` is still yaml-only (nested, ops-tuning) and must survive an
    editor save untouched. The media-key trio is FLAT and grid-editable, so the
    editor writes it like any other key — that flatness is what removed the
    special-casing this test used to guard."""
    from tests.test_ask_server import _server_with_node

    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    srv, ws = _server_with_node()
    srv._write_override_file(
        "k",
        {"watchdog": {"reexec_minutes": 10}, "room_id": "office",
         "volume_buttons": True, "volume_button_device": "SP300U"},  # fmt: skip
    )
    srv.write_node_override(
        "k", {"volume": 55, "volume_buttons": False, "volume_button_step": 8}
    )
    after = srv.read_node_override("k")
    assert after["watchdog"] == {"reexec_minutes": 10}  # yaml-only: preserved
    assert after["room_id"] == "office"  # server-managed: preserved
    assert after["volume_buttons"] is False  # flat: the editor owns it
    assert after["volume_button_step"] == 8
    assert "volume_button_device" not in after  # omitted by the editor = cleared, like any key


def test_media_key_trio_is_grid_editable(tmp_path, monkeypatch):
    """Config parity: the three keys must be in the same allow-list the node
    editor renders from — that's what puts them in the grid at all."""
    from tests.test_ask_server import _server_with_node

    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    srv, _ = _server_with_node()
    editable = set(srv.allowed_override_keys())
    assert {"volume_buttons", "volume_button_device", "volume_button_step"} <= editable


def test_probe_marks_devices_that_carry_volume_keys():
    """The wizard's join: a PortAudio device whose ALSA card sits on a USB
    device that also has volume keys is flagged, so the device step can offer
    the buttons while the user picks the speakerphone."""
    from kenzy.node.mediakeys import mark_volume_key_devices

    devices = [
        {"index": 2, "name": "SPEAKPHONE SP300U: USB Audio (hw:0,0)"},
        {"index": 3, "name": "HDA Intel PCH: CX20632 Analog (hw:1,0)"},
        {"index": 4, "name": "default"},
    ]
    mark_volume_key_devices(devices, {"1-3": "SPEAKPHONE SP300U"}, {0: "1-3", 1: ""})
    assert devices[0].get("volume_keys") is True
    # The exact endpoint rides along, so the wizard can store it instead of
    # leaving "auto" to re-derive it later (and fail on an alias/ambiguity).
    assert devices[0].get("volume_key_device") == "SPEAKPHONE SP300U"
    assert "volume_keys" not in devices[1]  # onboard audio: no USB parent
    assert "volume_keys" not in devices[2]  # an alias, no card in the name


# ---------------------------------------------------------------------------
# Auto tier 2: audio_device is "default" (unresolvable) — accept only the ONE
# volume-keyed endpoint whose USB parent also hosts a sound card.
# ---------------------------------------------------------------------------


def test_auto_fallback_picks_the_only_speakerphone_shaped_endpoint():
    choice, why = select_endpoint([KEYBOARD, SPK, TOUCHPAD], "", "auto", sound_parents={"1-3"})
    assert choice is SPK and "only audio-device endpoint" in why


def test_auto_fallback_keyboard_still_never_qualifies():
    # The keyboard HAS volume keys but its USB parent hosts no sound card.
    choice, why = select_endpoint([KEYBOARD, TOUCHPAD], "", "auto", sound_parents={"1-3"})
    assert choice is None


def test_auto_fallback_two_speakerphones_refuse_ambiguously():
    other = Candidate(
        path="/dev/input/event9", name="Other Speakerphone", phys="x",
        usb_parent="2-1", keys=frozenset({115}),
    )  # fmt: skip
    choice, why = select_endpoint([SPK, other], "", "auto", sound_parents={"1-3", "2-1"})
    assert choice is None and "ambiguous" in why


def test_auto_precise_tier_still_preferred_when_parent_known():
    # With the audio parent resolved, ONLY its siblings count — another
    # speakerphone elsewhere doesn't create ambiguity.
    other = Candidate(
        path="/dev/input/event9", name="Other Speakerphone", phys="x",
        usb_parent="2-1", keys=frozenset({115}),
    )  # fmt: skip
    choice, _ = select_endpoint([SPK, other], "1-3", "auto", sound_parents={"1-3", "2-1"})
    assert choice is SPK


# ---------------------------------------------------------------------------
# Dashboard configuration (config parity with the audio device — no yaml)
# ---------------------------------------------------------------------------


def test_candidates_payload_orders_audio_devices_first():
    from kenzy.node.mediakeys import candidates_payload

    out = candidates_payload([KEYBOARD, SPK, TOUCHPAD], {"1-3"})
    assert [c["name"] for c in out] == ["SPEAKPHONE SP300U", "CHICONY USB Keyboard"]
    assert out[0]["audio"] is True and out[1]["audio"] is False
    # The touchpad (no volume keys) never appears at all.


def test_no_editable_node_key_is_eaten_by_the_secret_filter():
    """The trap this feature fell into: `_effective_node_config` strips any key
    whose NAME contains key/token/secret/password/credential, so an innocent
    `media_volume_keys` was silently deleted from every served config — the
    node never learned it was enabled, and the only evidence was a warning
    line. Any editable key that can't survive the filter is unusable by
    definition, so the two lists must never intersect."""
    from kenzy.server.server import _ALLOWED_OVERRIDE_KEYS, _SECRET_KEY_RE

    eaten = sorted(k for k in _ALLOWED_OVERRIDE_KEYS if _SECRET_KEY_RE.search(k))
    assert not eaten, f"editable keys destroyed by the secret filter: {eaten}"


def test_node_defaults_survive_the_secret_filter():
    """Same trap, the other source: a packaged node_defaults key that trips the
    filter is dropped before it ever reaches a node."""
    import yaml

    from kenzy.config import packaged_config
    from kenzy.server.server import _SECRET_KEY_RE

    cfg = yaml.safe_load(packaged_config("server").read_text()) or {}
    eaten = sorted(k for k in (cfg.get("node_defaults") or {}) if _SECRET_KEY_RE.search(k))
    assert not eaten, f"node_defaults destroyed by the secret filter: {eaten}"


def test_no_visible_endpoints_blames_permissions_not_the_name():
    """The misdiagnosis that cost an afternoon: with /dev/input unreadable the
    scan returns NOTHING, and the explicit branch reported 'no input device
    matches <name>' — which reads as a wrong setting. An empty candidate list
    has exactly one likely cause and must say so."""
    choice, why = select_endpoint([], "1-3", "SP300U")
    assert choice is None and "input" in why and "group" in why
    # Same for auto — the cause doesn't depend on how the device was chosen.
    choice, why = select_endpoint([], "", "auto", sound_parents={"1-3"})
    assert choice is None and "group" in why
    # And a genuinely wrong name still says THAT, when devices are visible.
    choice, why = select_endpoint([SPK], "", "nosuchdevice")
    assert choice is None and "no input device matches" in why


def test_upgrade_extra_preserves_mediakeys_only_when_installed(monkeypatch):
    """A node's one-click upgrade runs `pip install -U kenzy[node]`. If it always
    added `mediakeys`, every upgrade would try to BUILD evdev — breaking nodes
    without gcc, the exact hazard that kept it out of the node extra. If it never
    added it, a node that has the feature would stop tracking evdev. So: carry it
    iff evdev already imports."""
    import builtins

    from kenzy.upgrade import pip_upgrade_command

    # The rule the client applies, exercised directly (importing the client's
    # upgrade branch needs a live WS session).
    def extra_for(evdev_present: bool) -> str:
        real_import = builtins.__import__

        def fake(name, *a, **k):
            if name == "evdev" and not evdev_present:
                raise ImportError("no evdev")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", fake)
        try:
            try:
                import evdev  # noqa: F401

                return "node,mediakeys"
            except ImportError:
                return "node"
        finally:
            monkeypatch.setattr(builtins, "__import__", real_import)

    assert extra_for(True) == "node,mediakeys"
    assert extra_for(False) == "node"
    # And the built argv is the plain pip form either way (constraints honored).
    cmd = pip_upgrade_command("node,mediakeys", None)
    assert "kenzy[node,mediakeys]>=3.0.0" in cmd and "-U" in cmd
