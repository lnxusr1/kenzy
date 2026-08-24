"""kenzy.s2s — the v6 interaction seam (design: kenzy-design/app/s2s-design.md).

A stable, Realtime-shaped boundary between Kenzy's last mile and swappable
conversation engines. The engine behind the seam is expected to change
(cascade orchestrator today, audio-native model later); what this package owns
is the part that must NOT change with it:

- :mod:`kenzy.s2s.profiles` — per-engine normalization: the measured behavioral
  divergences between engines that share the same protocol shape (whether
  ``response.create`` is required or auto-cancelled, voice namespaces, whether
  engine-side input transcription exists at all).
- :mod:`kenzy.s2s.engine` — the engine client: streams audio in, commits the
  turn (Kenzy owns endpointing — seam decision 1), surfaces a small set of
  typed events out, and can cancel a response mid-stream (the fast-path
  interceptor's lever).

No Kenzy policy lives here — the gate, identity, lockbox, and delivery sit
ABOVE this package, engine-independent by construction.
"""

from kenzy.s2s.engine import (
    AudioDelta,
    EngineClient,
    EngineError,
    EngineEvent,
    InputTranscript,
    ReplyTranscriptDelta,
    ResponseDone,
    ResponseStarted,
    SessionReady,
    ToolCall,
)
from kenzy.s2s.gate import (
    AuditRecord,
    SecretDiversion,
    Speaker,
    ToolRule,
    ToolVerdict,
    TurnGate,
)
from kenzy.s2s.profiles import HF_LOCAL, OPENAI_REALTIME, EngineProfile

__all__ = [
    "HF_LOCAL",
    "OPENAI_REALTIME",
    "AudioDelta",
    "AuditRecord",
    "EngineClient",
    "EngineError",
    "EngineEvent",
    "EngineProfile",
    "InputTranscript",
    "ReplyTranscriptDelta",
    "ResponseDone",
    "ResponseStarted",
    "SecretDiversion",
    "SessionReady",
    "Speaker",
    "ToolCall",
    "ToolRule",
    "ToolVerdict",
    "TurnGate",
]
