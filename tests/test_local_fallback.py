"""Silent local fallback for the cloud stages (everyday-essentials item 3):
LLM → configured local model, STT openai → lazy local whisper, TTS openai →
lazy Kokoro. Semantics per operator decision: one silent retry — if the
fallback also fails, the exception propagates and the user just gets the
pre-recorded error cue."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import litellm
import pytest

from kenzy.llm import skills as sk


@pytest.fixture(autouse=True)
def _clean_fallback():
    yield
    sk.set_fallback(None, None)


def _resp(text: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


# ---------------------------------------------------------------------------
# LLM: acompletion_with_fallback
# ---------------------------------------------------------------------------


async def test_llm_falls_back_and_pins_state(monkeypatch):
    calls: list[dict] = []

    async def fake(**kwargs):
        calls.append(kwargs)
        if kwargs["model"] == "gpt-cloud":
            raise OSError("cloud down")
        return _resp("local answer")

    monkeypatch.setattr(litellm, "acompletion", fake)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    sk.set_fallback("ollama/local", "http://127.0.0.1:11434")

    state: dict = {}
    r = await sk.acompletion_with_fallback({"model": "gpt-cloud", "messages": []}, state)
    assert r.choices[0].message.content == "local answer"
    assert [c["model"] for c in calls] == ["gpt-cloud", "ollama/local"]
    # The fallback call must carry the local base_url and never the OpenAI key.
    assert calls[1]["base_url"] == "http://127.0.0.1:11434"
    assert calls[1].get("api_key") != "sk-real"

    # Pinned: the next call in the same request skips the dead primary.
    await sk.acompletion_with_fallback({"model": "gpt-cloud", "messages": []}, state)
    assert [c["model"] for c in calls][-1] == "ollama/local"
    assert len(calls) == 3


async def test_llm_no_fallback_configured_reraises(monkeypatch):
    async def fake(**kwargs):
        raise OSError("cloud down")

    monkeypatch.setattr(litellm, "acompletion", fake)
    sk.set_fallback(None, None)
    with pytest.raises(OSError):
        await sk.acompletion_with_fallback({"model": "gpt-cloud", "messages": []}, {})


async def test_llm_fallback_failure_is_silent_reraise(monkeypatch):
    async def fake(**kwargs):
        raise OSError(f"down: {kwargs['model']}")

    monkeypatch.setattr(litellm, "acompletion", fake)
    sk.set_fallback("ollama/local", None)
    with pytest.raises(OSError, match="ollama/local"):
        await sk.acompletion_with_fallback({"model": "gpt-cloud", "messages": []}, {})


async def test_llm_primary_success_never_touches_fallback(monkeypatch):
    calls: list[str] = []

    async def fake(**kwargs):
        calls.append(kwargs["model"])
        return _resp("ok")

    monkeypatch.setattr(litellm, "acompletion", fake)
    sk.set_fallback("ollama/local", None)
    await sk.acompletion_with_fallback({"model": "gpt-cloud", "messages": []}, {})
    assert calls == ["gpt-cloud"]


# ---------------------------------------------------------------------------
# STT: openai → lazy local whisper
# ---------------------------------------------------------------------------


async def test_stt_falls_back_to_lazy_whisper(monkeypatch):
    from kenzy.stt import stt as svc

    class _FailingOpenAI:
        class audio:  # noqa: N801
            class transcriptions:  # noqa: N801
                @staticmethod
                def create(**kwargs):
                    raise OSError("cloud down")

    class _FakeSegment:
        text = "local transcript"

    class _FakeWhisper:
        def transcribe(self, audio, language=None, beam_size=5):
            return [_FakeSegment()], None

    monkeypatch.setattr(svc, "_provider", "openai")
    monkeypatch.setattr(svc, "_openai_client", _FailingOpenAI())
    monkeypatch.setattr(svc, "_openai_fallback", True)
    monkeypatch.setattr(svc, "_whisper", None)
    monkeypatch.setattr(svc, "_sem", asyncio.Semaphore(1))
    monkeypatch.setattr(svc, "_load_whisper", lambda: _FakeWhisper())

    from base64 import b64encode

    from fastapi.testclient import TestClient

    r = TestClient(svc.app).post(
        "/transcribe", json={"audio_b64": b64encode(b"\x01\x00" * 1600).decode()}
    )
    assert r.status_code == 200 and r.json()["text"] == "local transcript"

    # Disabled fallback → the cloud failure propagates (the server plays the cue).
    monkeypatch.setattr(svc, "_openai_fallback", False)
    with pytest.raises(OSError):
        TestClient(svc.app, raise_server_exceptions=True).post(
            "/transcribe", json={"audio_b64": b64encode(b"\x01\x00" * 1600).decode()}
        )


# ---------------------------------------------------------------------------
# TTS: openai → lazy Kokoro
# ---------------------------------------------------------------------------


def test_tts_falls_back_to_kokoro(monkeypatch):
    from kenzy.tts import tts as svc

    def boom(text, voice_prompt):
        raise OSError("cloud down")

    monkeypatch.setattr(svc, "_provider", "openai")
    monkeypatch.setattr(svc, "_openai_fallback", True)
    monkeypatch.setattr(svc, "_synthesise_openai", boom)
    monkeypatch.setattr(svc, "_ensure_fallback_kokoro", lambda: None)
    monkeypatch.setattr(svc, "_synthesise_kokoro", lambda text: b"LOCALPCM")
    assert svc._synthesise("hello", "vp") == b"LOCALPCM"

    # Kokoro not installed (ImportError from the lazy load) → propagates.
    def no_kokoro():
        raise ImportError("kokoro not installed")

    monkeypatch.setattr(svc, "_ensure_fallback_kokoro", no_kokoro)
    with pytest.raises(ImportError):
        svc._synthesise("hello", "vp")

    # Fallback disabled → the original cloud error propagates.
    monkeypatch.setattr(svc, "_openai_fallback", False)
    with pytest.raises(OSError):
        svc._synthesise("hello", "vp")
