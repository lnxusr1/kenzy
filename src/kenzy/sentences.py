"""Sentence-boundary splitting, shared.

Two consumers: the classic pipeline's 4.4 streaming aggregator
(``kenzy.server.server``) and the s2s engine's sentence-streamed synthesis
(``kenzy.s2s.server``). ONE splitter on purpose — the two paths must never
disagree about what a sentence is, and the s2s service must not import the
server module to borrow it.
"""

from __future__ import annotations

import re

#: Terminator (+ closing quotes/brackets) followed by whitespace. Decimals
#: ("93.5") never match — no whitespace after the dot.
SENT_END_RE = re.compile(r"[.!?…]+[\"'”’)\]]*\s+")


def split_sentences(buf: str) -> tuple[list[str], str]:
    """Split complete raw sentence slices off the front of ``buf``.

    Slices keep their trailing whitespace so ``"".join(slices) + remainder ==
    buf`` EXACTLY — the 4.4 spoken-prefix bookkeeping relies on byte equality
    with the authoritative end-event text.
    """
    out: list[str] = []
    start = 0
    for m in SENT_END_RE.finditer(buf):
        out.append(buf[start : m.end()])
        start = m.end()
    return out, buf[start:]
