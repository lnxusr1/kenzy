"""The 4.2 sound system: library-root resolution (the security boundary),
count-shaped repeats, the HTTP /chime twin, and MP3 decode (skipped without
the optional `av`)."""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from kenzy.server import tones
from kenzy.server.server import NodeSession, TranscribingServer


def _wav(path: Path, seconds: float = 0.2, rate: int = 24000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x01\x02" * int(rate * seconds))
    return path


# ---------------------------------------------------------------------------
# resolve_sound — the boundary
# ---------------------------------------------------------------------------


def test_resolve_sound_within_roots(tmp_path):
    root = tmp_path / "sounds"
    _wav(root / "dog.wav")
    _wav(root / "alerts" / "bell.wav")
    assert tones.resolve_sound("dog.wav", [root]) == (root / "dog.wav").resolve()
    # Relative subpaths are fine.
    bell = (root / "alerts" / "bell.wav").resolve()
    assert tones.resolve_sound("alerts/bell.wav", [root]) == bell
    # First root with the file wins.
    other = tmp_path / "other"
    _wav(other / "dog.wav")
    assert tones.resolve_sound("dog.wav", [other, root]) == (other / "dog.wav").resolve()
    # Missing file → None.
    assert tones.resolve_sound("cat.wav", [root]) is None


def test_resolve_sound_rejects_escapes(tmp_path):
    root = tmp_path / "sounds"
    _wav(root / "dog.wav")
    secret = _wav(tmp_path / "outside.wav")
    assert tones.resolve_sound("../outside.wav", [root]) is None  # traversal
    assert tones.resolve_sound(str(secret), [root]) is None  # absolute path
    assert tones.resolve_sound(".hidden.wav", [root]) is None
    assert tones.resolve_sound("", [root]) is None
    # A symlink pointing outside the root is refused too.
    link = root / "sneaky.wav"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("no symlink support")
    assert tones.resolve_sound("sneaky.wav", [root]) is None


def test_repeat_pcm_counts_and_caps():
    one = b"\x01\x02" * 2400  # 0.1s at 24k
    assert tones.repeat_pcm(one, 3) == one * 3
    assert tones.repeat_pcm(one, 1) == one
    assert tones.repeat_pcm(one, 0) == one
    # The duration cap bounds absurd counts.
    capped = tones.repeat_pcm(one, 10_000, max_seconds=1.0)
    assert len(capped) <= len(one) * 11


# ---------------------------------------------------------------------------
# Server: _chime_spec through the roots + play_chime repeats + HTTP twin
# ---------------------------------------------------------------------------


class _WS:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, m: object) -> None:
        self.sent.append(m)


def _server(tmp_path, monkeypatch, sounds_dirs=None):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    cfg: dict = {"discovery": {"enabled": False}}
    if sounds_dirs is not None:
        cfg["sounds"] = {"dirs": sounds_dirs}
    s = TranscribingServer(cfg)
    s._nodes["n1"] = NodeSession(ws=_WS(), node_id="n1", room_id="office")  # type: ignore[arg-type]
    return s


async def test_chime_spec_resolution_order(tmp_path, monkeypatch):
    lib = tmp_path / "library"
    _wav(lib / "dog bark.wav")
    _wav(tmp_path / "data" / "sounds" / "knock.wav")
    s = _server(tmp_path, monkeypatch, sounds_dirs=[str(lib)])
    s._chimes = {"doorbell": "doorbell.wav"}

    assert s._chime_spec("doorbell") == "doorbell.wav"  # alias map first
    assert s._chime_spec("knock.wav") == str((tmp_path / "data/sounds/knock.wav").resolve())
    assert s._chime_spec("dog bark.wav") == str((lib / "dog bark.wav").resolve())
    assert s._chime_spec("ready.wav") == "ready.wav"  # bundled bare name passthrough
    assert s._chime_spec("../etc/passwd") is None
    assert s._chime_spec("/etc/passwd") is None


async def test_play_chime_repeats(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    streamed: list[bytes] = []

    async def fake_stream(node_id, pcm, alert=False):
        streamed.append(pcm)

    monkeypatch.setattr(s, "_stream_pcm", fake_stream)
    one = tones.load_tone("ready.wav")
    assert one
    count = await s.play_chime("ready.wav", repeats=3)
    assert count == 1
    assert len(streamed[0]) == len(one) * 3


async def test_http_chime_twin(tmp_path, monkeypatch):
    _wav(tmp_path / "data" / "sounds" / "knock.wav")  # resolvable in the library
    s = _server(tmp_path, monkeypatch)
    played: list[tuple] = []

    async def fake_play(sound=None, seconds=0.0, rooms=None, repeats=0):
        played.append((sound, seconds, rooms, repeats))
        return 1

    monkeypatch.setattr(s, "play_chime", fake_play)
    monkeypatch.setattr(s, "_check_service_token", lambda req: True)

    class _Req:
        path = "/chime?sound=knock.wav&repeats=2&rooms=office,den"

    resp = await s._http_chime(_Req())
    assert resp.status_code == 200
    assert played == [("knock.wav", 0.0, ["office", "den"], 2)]

    class _Bad:
        path = "/chime?seconds=abc"

    resp = await s._http_chime(_Bad())
    assert resp.status_code == 400


async def test_http_chime_unknown_sound_404(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "_check_service_token", lambda req: True)

    class _Req:
        path = "/chime?sound=nope.wav"

    resp = await s._http_chime(_Req())
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# MP3 decode (optional av)
# ---------------------------------------------------------------------------


def test_mp3_loads_via_av(tmp_path):
    av = pytest.importorskip("av")
    # Synthesize a tiny MP3 with av itself, then load it through the tone path.
    mp3 = tmp_path / "beep.mp3"
    import numpy as np  # av ships with numpy support in test envs; skip if absent

    rate = 24000
    t = np.arange(rate // 4) / rate
    samples = (np.sin(2 * np.pi * 440 * t) * 12000).astype("int16").reshape(1, -1)
    with av.open(str(mp3), "w") as out:
        stream = out.add_stream("mp3", rate=rate)
        frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
        frame.sample_rate = rate
        for packet in stream.encode(frame):
            out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)

    pcm = tones.load_tone(str(mp3))
    assert pcm and len(pcm) > 8000  # ~0.25s of 24k mono int16


def test_missing_av_degrades_honestly(tmp_path, monkeypatch):
    from kenzy import soundfile

    monkeypatch.setattr(soundfile, "available", lambda: False)
    fake = tmp_path / "x.mp3"
    fake.write_bytes(b"not audio")
    # decode() itself guards on import; simulate absence via import failure.
    import builtins

    real_import = builtins.__import__

    def block_av(name, *a, **k):
        if name == "av":
            raise ImportError("blocked")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", block_av)
    assert soundfile.decode(fake, rate=24000) is None
