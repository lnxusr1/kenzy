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
- Transcript sourcing is ONE method for every engine (founder, 2026-08-29 —
  "there shouldn't be two different methods"): the engine transcribes its own
  input and the gate rides its transcript events, local and cloud alike. No
  front or parallel kenzy-stt preemption exists. The cloud engine's measured
  late-transcript race (transcripts can arrive after ``response.done``) is
  why the gate's transcript wait fails CLOSED on timeout rather than acting
  untranscribed.
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
    #: Ask the engine (via session.update) to transcribe input audio itself.
    #: The gate rides engine transcript events on EVERY profile (the one
    #: transcript method); this flag exists because some engines transcribe
    #: only when asked (OpenAI GA) while STT-driven ones do it inherently.
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

    def map_voice(self, canonical: str) -> str:
        """Resolve the configured voice identity to this engine's namespace."""
        if self.passthrough_voice and canonical:
            return canonical
        return self.voice_map.get(canonical, self.default_voice)


OPENAI_REALTIME = EngineProfile(
    name="openai-realtime",
    url="wss://api.openai.com/v1/realtime",
    requires_response_create=True,
    # Ingress ruled 2026-08-29: the cloud engine gets ALL the audio and calls
    # tools itself — the same shape as the local engine, one method (no
    # text-in-front, no parallel kenzy-stt). The engine transcribes its own
    # input (asked via session.update) and the gate rides those transcript
    # events; its measured late-transcript race is covered by the gate's
    # fail-closed wait. The trade — audio egresses with whatever it contains,
    # secrets included — is a documented caveat of the cloud OPT-IN, never a
    # blocker; the default engine is local.
    engine_transcription=True,
    voice_map={"bm_fable": "marin"},
    default_voice="marin",
)

HF_LOCAL = EngineProfile(
    name="hf-s2s",
    url="ws://127.0.0.1:8765/v1/realtime",
    requires_response_create=False,
    voice_map={"marin": "bm_fable"},
    default_voice="bm_fable",
    auth="none",
)

KENZY_S2S = EngineProfile(  # the default — see PROFILES below
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

#: The `s2s.profile` config vocabulary — what the server's selector resolves
#: through. "kenzy" is the default (local engine); "openai-realtime" is the
#: cloud opt-in (v6.0 requirement, 2026-08-29 — its audio-egress trade is a
#: documented caveat); "hf" is the probed HF speech-to-speech server (dev).
PROFILES: dict[str, EngineProfile] = {
    "kenzy": KENZY_S2S,
    "openai-realtime": OPENAI_REALTIME,
    "hf": HF_LOCAL,
}
