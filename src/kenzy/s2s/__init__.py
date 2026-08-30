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
  typed events out, and can cancel a response mid-stream.
- :mod:`kenzy.s2s.gate` — the authority checkpoint: transcript-before-action,
  the secret screen, identity at action time, tool verdicts, delivery release.
- :mod:`kenzy.s2s.identity` — session identity: monotonic-add speaker set for
  the world model, current-speaker binding for the gate (decision 6).
- :mod:`kenzy.s2s.conversation` — the lifecycle: follow-up windows anchored at
  node playback-complete, the five ends (the ``end_conversation`` tool among
  them), and the turn runner with the measured result-sequencing rule.
- :mod:`kenzy.s2s.ledger` — the task ledger: detached work, owner-scoped,
  restart-safe, honest about failure.

No Kenzy policy lives in the engine layer — the gate, identity, lockbox, and
delivery sit ABOVE the seam, engine-independent by construction.
"""

from kenzy.s2s.conversation import (
    END_CONVERSATION,
    END_CONVERSATION_TOOL,
    ConversationSession,
    TurnResult,
    TurnRunner,
    WindowPolicy,
    end_conversation_rule,
)
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
from kenzy.s2s.identity import PersonHeard, SessionIdentity
from kenzy.s2s.ledger import Task, TaskLedger
from kenzy.s2s.profiles import HF_LOCAL, KENZY_S2S, OPENAI_REALTIME, EngineProfile

__all__ = [
    "END_CONVERSATION",
    "END_CONVERSATION_TOOL",
    "HF_LOCAL",
    "KENZY_S2S",
    "OPENAI_REALTIME",
    "AudioDelta",
    "AuditRecord",
    "ConversationSession",
    "EngineClient",
    "EngineError",
    "EngineEvent",
    "EngineProfile",
    "InputTranscript",
    "PersonHeard",
    "ReplyTranscriptDelta",
    "ResponseDone",
    "ResponseStarted",
    "SecretDiversion",
    "SessionIdentity",
    "SessionReady",
    "Speaker",
    "Task",
    "TaskLedger",
    "ToolCall",
    "ToolRule",
    "ToolVerdict",
    "TurnGate",
    "TurnResult",
    "TurnRunner",
    "WindowPolicy",
    "end_conversation_rule",
]
