"""Per-engine normalization profiles for the interaction seam.

Two engines can share the Realtime protocol SHAPE and still diverge in
behavior — every field here exists because the divergence was MEASURED, not
assumed (2026-08-22, ``scripts/realtime_probe.py``; findings in
kenzy-design/app/s2s-design.md):

- OpenAI's GA API requires an explicit ``response.create`` after the buffer
  commit; the HF ``speech-to-speech`` Realtime server is STT-driven and
  auto-responds — a commit-time ``response.create`` there is auto-CANCELLED
  (0 tokens), silently eating the turn.
- Voice namespaces are disjoint (``marin`` vs Kokoro's ``bm_fable``), and an
  unknown voice name can 404 deep inside an engine. Seam decision 5 (one
  configured voice identity) therefore needs a per-engine mapping, never a
  pass-through string.
- Engine-side input transcription is OFF by default everywhere: Kenzy sources
  its own transcript (parallel kenzy-stt — ~4x faster than the engine's
  attached transcription and the trust organ besides). Tests may enable it to
  exercise the late-transcript race the cloud engine exhibits.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class EngineProfile:
    """The measured behavioral contract of one engine behind the seam."""

    name: str
    #: Default WebSocket endpoint (the ``?model=`` query is appended by the client).
    url: str
    #: True: the engine waits for an explicit ``response.create`` after commit
    #: (OpenAI GA). False: the engine is STT-driven and auto-responds — sending
    #: ``response.create`` at commit gets it auto-cancelled (HF s2s server).
    requires_response_create: bool
    #: Ask the engine to transcribe input audio itself. Default False — Kenzy
    #: sources transcripts from kenzy-stt in parallel (seam decision 3/4).
    engine_transcription: bool = False
    #: Canonical (Kenzy-configured) voice -> this engine's voice name.
    voice_map: Mapping[str, str] = field(default_factory=dict)
    #: Used when the canonical voice has no mapping — never pass an unmapped
    #: name through (measured: unknown voices 404 inside engines).
    default_voice: str = ""
    #: True: the engine shares Kenzy's own voice namespace (kenzy-s2s — its TTS
    #: IS kenzy-tts, decision 8), so the canonical name passes through intact.
    passthrough_voice: bool = False
    #: "bearer" sends Authorization from the api key; "none" for local servers.
    auth: str = "bearer"
    #: Where the GATE's transcript comes from (the invariant — transcript on
    #: record before any action — never changes; only the sourcing does):
    #: "engine"   — ride the engine's native transcript events (cascade engines:
    #:              transcript-first by construction, and their STT stage IS
    #:              kenzy-stt, so a parallel pass would run the model twice);
    #: "front"    — kenzy-stt runs serially BEFORE the engine (the cloud
    #:              text-in/audio-out ingress path);
    #: "parallel" — kenzy-stt races the engine on the same committed audio (the
    #:              adapter that lets a future audio-native engine qualify).
    transcript_source: str = "engine"

    def map_voice(self, canonical: str) -> str:
        """Resolve the configured voice identity to this engine's namespace."""
        if self.passthrough_voice and canonical:
            return canonical
        return self.voice_map.get(canonical, self.default_voice)


OPENAI_REALTIME = EngineProfile(
    name="openai-realtime",
    url="wss://api.openai.com/v1/realtime",
    requires_response_create=True,
    voice_map={"bm_fable": "marin"},
    default_voice="marin",
    # The ingress decision: the cloud path is text-in/audio-out — kenzy-stt
    # runs in FRONT (lockbox screen before anything leaves the house; raw user
    # audio never streams to a cloud engine). Audio-in against this profile is
    # probe/experiment territory only.
    transcript_source="front",
)

HF_LOCAL = EngineProfile(
    name="hf-s2s",
    url="ws://127.0.0.1:8765/v1/realtime",
    requires_response_create=False,
    voice_map={"marin": "bm_fable"},
    default_voice="bm_fable",
    auth="none",
)

KENZY_S2S = EngineProfile(
    name="kenzy-s2s",
    url="ws://127.0.0.1:8771/v1/realtime",
    # Our own engine is GA-conformant BY CHOICE: commit does not auto-respond
    # (the seam's north-star shape), and the transcript is emitted before any
    # output exists — the qualifying bar met by construction, not adaptation.
    requires_response_create=True,
    engine_transcription=True,
    passthrough_voice=True,
    default_voice="bm_fable",
    auth="none",
)
