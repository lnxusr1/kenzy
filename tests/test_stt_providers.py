"""Tests for the STT service's provider split (local whisper vs OpenAI cloud).

No models are loaded — the whisper path is exercised with a stub model object and
the openai path with a stub client, via the same module-global monkeypatching the
service's main() performs.
"""

from __future__ import annotations

import asyncio
import base64
import io
import wave

import pytest
from fastapi.testclient import TestClient

from kenzy.stt import stt as svc

PCM = (b"\x01\x00" * 1600) * 5  # 0.5 s of 16 kHz int16 mono


def test_pcm_to_wav_is_a_valid_wav_container():
    data = svc._pcm_to_wav(PCM)
    with wave.open(io.BytesIO(data), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16000
        assert wav.readframes(wav.getnframes()) == PCM


class _FakeTranscriptions:
    def __init__(self, text: str):
        self._text = text
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)

        class _Result:
            text = self._text

        return _Result()


class _FakeOpenAI:
    def __init__(self, text: str = "  hello world  "):
        self.transcriptions = _FakeTranscriptions(text)

    @property
    def audio(self):
        return self


@pytest.fixture
def openai_svc(monkeypatch):
    fake = _FakeOpenAI()
    monkeypatch.setattr(svc, "_provider", "openai")
    monkeypatch.setattr(svc, "_openai_client", fake)
    monkeypatch.setattr(svc, "_openai_model", "gpt-4o-mini-transcribe")
    monkeypatch.setattr(svc, "_language", "en")
    return fake


def test_run_openai_sends_wav_and_language(openai_svc):
    text = svc._run_openai(PCM)
    assert text == "hello world"
    (call,) = openai_svc.transcriptions.calls
    assert call["model"] == "gpt-4o-mini-transcribe"
    assert call["language"] == "en"
    filename, wav_bytes = call["file"]
    assert filename == "audio.wav"
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        assert wav.readframes(wav.getnframes()) == PCM


def test_run_openai_omits_language_when_auto(openai_svc, monkeypatch):
    monkeypatch.setattr(svc, "_language", None)
    svc._run_openai(PCM)
    (call,) = openai_svc.transcriptions.calls
    assert "language" not in call


def test_transcribe_endpoint_dispatches_to_openai(openai_svc, monkeypatch):
    # The whisper semaphore is deliberately absent — the openai path must not need it.
    monkeypatch.setattr(svc, "_sem", None)
    client = TestClient(svc.app)
    r = client.post("/transcribe", json={"audio_b64": base64.b64encode(PCM).decode()})
    assert r.status_code == 200
    assert r.json()["text"] == "hello world"


def test_transcribe_endpoint_whisper_path(monkeypatch):
    class _FakeSegment:
        def __init__(self, text: str):
            self.text = text

    class _FakeWhisper:
        def transcribe(self, audio, language=None, beam_size=5):
            return [_FakeSegment(" turn on "), _FakeSegment("the lights ")], None

    monkeypatch.setattr(svc, "_provider", "whisper")
    monkeypatch.setattr(svc, "_whisper", _FakeWhisper())
    monkeypatch.setattr(svc, "_sem", asyncio.Semaphore(1))
    client = TestClient(svc.app)
    r = client.post("/transcribe", json={"audio_b64": base64.b64encode(PCM).decode()})
    assert r.status_code == 200
    assert r.json()["text"] == "turn on the lights"


def test_health_reports_provider_and_model(monkeypatch):
    monkeypatch.setattr(svc, "_provider", "openai")
    monkeypatch.setattr(svc, "_openai_model", "gpt-4o-mini-transcribe")
    monkeypatch.setattr(svc, "_language", None)
    body = TestClient(svc.app).get("/health").json()
    assert body["provider"] == "openai"
    assert body["model"] == "gpt-4o-mini-transcribe"
    assert body["language"] == "auto"

    monkeypatch.setattr(svc, "_provider", "whisper")
    monkeypatch.setattr(svc, "_model_size", "tiny")
    monkeypatch.setattr(svc, "_language", "en")
    body = TestClient(svc.app).get("/health").json()
    assert body["provider"] == "whisper"
    assert body["model"] == "tiny"
    assert body["language"] == "en"


async def test_gpu_inference_failure_rescues_to_cpu(monkeypatch):
    """CUDA models build fine and then die on the FIRST transcribe when cuDNN
    is missing — the field failure mode (mouse, 4.2.0: every request 500'd).
    One loud rescue onto the CPU, latched, and health reports the fallback."""
    import asyncio

    from kenzy.stt import stt

    monkeypatch.setattr(stt, "_wcfg", {"model": "small", "device": "cuda", "compute_type": "int8"})
    monkeypatch.setattr(stt, "_sem", asyncio.Semaphore(1))
    monkeypatch.setattr(stt, "_gpu_failed", False)

    class _GpuModel:
        def transcribe(self, audio, **kw):
            raise RuntimeError("Library libcudnn_ops not found")

    class _CpuModel:
        def transcribe(self, audio, **kw):
            class _Seg:
                text = "hello"

            return [_Seg()], None

    loads = []

    def fake_load(device=None):
        loads.append(device)
        return _CpuModel()

    monkeypatch.setattr(stt, "_whisper", _GpuModel())
    monkeypatch.setattr(stt, "_load_whisper", fake_load)

    loop = asyncio.get_running_loop()
    text = await stt._local_transcribe(b"\x00\x00" * 1600, loop)
    assert text == "hello"
    assert loads == ["cpu"]  # rebuilt explicitly on CPU
    assert stt._gpu_failed is True

    # Latched: a later failure on the CPU model raises instead of looping.
    monkeypatch.setattr(stt, "_whisper", _GpuModel())
    import pytest as _pytest

    with _pytest.raises(RuntimeError):
        await stt._local_transcribe(b"\x00\x00" * 1600, loop)


async def test_cpu_failure_logs_and_raises(monkeypatch, caplog):
    import asyncio
    import logging

    from kenzy.stt import stt

    monkeypatch.setattr(stt, "_wcfg", {"device": "cpu"})
    monkeypatch.setattr(stt, "_sem", asyncio.Semaphore(1))
    monkeypatch.setattr(stt, "_gpu_failed", False)

    class _Bad:
        def transcribe(self, audio, **kw):
            raise RuntimeError("boom")

    monkeypatch.setattr(stt, "_whisper", _Bad())
    with caplog.at_level(logging.ERROR, logger="kenzy.stt.stt"):
        import pytest as _pytest

        with _pytest.raises(RuntimeError):
            await stt._local_transcribe(b"\x00\x00" * 1600, asyncio.get_running_loop())
    assert any("Transcription failed" in r.message for r in caplog.records)
