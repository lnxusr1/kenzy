"""Secret-shaped log redaction (4.1 lockbox).

A spoken secret transits STT, the server, and the LLM service before the
lockbox ever sees it — and each used to INFO-log the transcript, landing the
value in journald and the dashboard's Logs tab in plaintext. This tiny
base-package module gives every service the same deterministic redaction
without any cross-service import (the same wire-contract rule as the tier
constants): a transcript is withheld when it is *certainly* secret-shaped —
a credential word plus a code-like payload, the classifier's own "vault it"
heuristic. Over-redacting a log line costs nothing; under-redacting is a
plaintext credential on disk.
"""

from __future__ import annotations

import re

#: Strong secret signals: the text names a credential-shaped thing.
SECRET_WORDS_RE = re.compile(
    r"\b(password|passcode|passphrase|pin\b|pin number|combination|combo|"
    r"secret|access code|gate code|door code|garage code|security code|"
    r"safe code|lock(?:er)? code|unlock code|key ?code|api key|token)\b",
    re.IGNORECASE,
)
#: A code-like payload: digit runs (commas/periods included — STT writes
#: "6,000"), or mixed letter-digit blobs.
PAYLOAD_RE = re.compile(r"\b(?:\d[\s,.\-]?){4,}|\b(?=\w*\d)(?=\w*[a-zA-Z])\w{6,}\b")

WITHHELD = "[secret-shaped utterance — content withheld]"


def loggable(text: str) -> str:
    """``text`` unless it is certainly secret-shaped, then a fixed marker."""
    if SECRET_WORDS_RE.search(text) and PAYLOAD_RE.search(text):
        return WITHHELD
    return text
