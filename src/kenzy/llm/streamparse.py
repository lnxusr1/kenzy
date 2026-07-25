"""Incremental extraction of the reply contract from a streaming model response.

The reply contract is one JSON object — ``{"voice_prompt": …,
"expect_response": …, "text": "…"}`` with ``text`` deliberately LAST in the
schema property order — so a streaming completion delivers the two header
fields before the spoken text begins. :class:`StreamExtract` eats raw content
deltas and hands back pieces of the ``text`` string as they arrive (unescaped),
exposing the header the moment the text value opens.

Anything that doesn't follow the contract — a prompt-tier provider answering in
plain prose, markdown fences, truncated output — makes the extractor **bail**:
the caller stops streaming, buffers the full content, and runs the existing
lenient ``_parse_response`` on it. Always correct, just not incremental. A
reply whose ``text`` arrives before the header still streams; the header is
simply unavailable until the end (the caller uses its defaults).
"""

from __future__ import annotations

import json
import re
from typing import Any

# Escape shorthands inside a JSON string (\uXXXX handled separately).
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
_TEXT_KEY_RE = re.compile(r'"text"\s*:\s*"')


class StreamExtract:
    """Feed content deltas in; get spoken-text pieces out.

    States: PRE (scanning for the ``"text"`` string value), TEXT (inside it,
    unescaping), POST (after its closing quote). ``bailed`` means the content
    isn't contract-shaped — buffer everything and parse at the end.
    """

    def __init__(self) -> None:
        self.buf = ""  # every char seen — also the caller's bail/finalize record
        self.head: dict[str, Any] | None = None  # header fields once known
        self.done = False  # text string closed
        self.bailed = False
        self._state = "pre"
        self._scan_from = 0  # PRE: resume offset for the text-key regex
        self._esc = False  # TEXT: previous char was a backslash
        self._unicode = ""  # TEXT: partial \uXXXX assembly ("" = not in one)
        self._high_surrogate: int | None = None  # TEXT: pending UTF-16 high half
        self._checked_first = False

    def feed(self, delta: str) -> str:
        """Consume one content delta; return the newly-available text piece."""
        if not delta or self.bailed:
            self.buf += delta
            return ""
        self.buf += delta
        if not self._checked_first:
            head = self.buf.lstrip()
            if head:
                self._checked_first = True
                if head[0] != "{":
                    self.bailed = True  # not a JSON object — plain-prose reply
                    return ""
        if self._state == "pre":
            return self._scan_pre()
        if self._state == "text":
            return self._consume_text(delta)
        return ""  # post: trailing fields ignored until finalize

    # -- PRE: find where the text string value opens -----------------------

    def _scan_pre(self) -> str:
        m = _TEXT_KEY_RE.search(self.buf, self._scan_from)
        if m is None:
            # Keep a small overlap so a key split across deltas still matches.
            self._scan_from = max(0, len(self.buf) - 12)
            return ""
        self._parse_head(self.buf[: m.start()])
        self._state = "text"
        # Everything already buffered past the opening quote is text content.
        return self._consume_text(self.buf[m.end() :])

    def _parse_head(self, prefix: str) -> None:
        """The JSON before the ``"text"`` key, completed with a stub and parsed.

        ``{"voice_prompt": "…", "expect_response": false, `` → a full object.
        Failure just leaves ``head`` None (defaults until the end)."""
        candidate = prefix.rstrip()
        if candidate.endswith(","):
            candidate = candidate[:-1]
        if not candidate.endswith("{"):
            candidate += ","
        try:
            parsed = json.loads(candidate + ' "__stub__": 0}')
            parsed.pop("__stub__", None)
            self.head = parsed
        except (json.JSONDecodeError, ValueError):
            self.head = None

    # -- TEXT: stream the string value out, unescaping ---------------------

    def _consume_text(self, piece: str) -> str:
        out: list[str] = []
        for ch in piece:
            if self.done:
                break
            if self._unicode:
                self._unicode += ch
                if len(self._unicode) == 5:  # "u" + 4 hex digits
                    try:
                        cp = int(self._unicode[1:], 16)
                    except ValueError:
                        self.bailed = True
                        return "".join(out)
                    self._unicode = ""
                    if self._high_surrogate is not None:
                        if 0xDC00 <= cp <= 0xDFFF:  # combine the UTF-16 pair
                            out.append(
                                chr(0x10000 + ((self._high_surrogate - 0xD800) << 10) + cp - 0xDC00)
                            )
                            self._high_surrogate = None
                            continue
                        self.bailed = True  # lone high surrogate — malformed
                        return "".join(out)
                    if 0xD800 <= cp <= 0xDBFF:
                        self._high_surrogate = cp
                        continue
                    out.append(chr(cp))
                continue
            if self._esc:
                self._esc = False
                if ch == "u":
                    self._unicode = "u"
                elif ch in _ESCAPES:
                    out.append(_ESCAPES[ch])
                else:
                    self.bailed = True
                    return "".join(out)
                continue
            if ch == "\\":
                self._esc = True
            elif ch == '"':
                self.done = True
                self._state = "post"
            else:
                out.append(ch)
        return "".join(out)

    # -- finalize -----------------------------------------------------------

    def finalize(self) -> tuple[str, str, bool] | None:
        """Parse the COMPLETE buffered content after the stream ends.

        Returns ``(text, voice_prompt, expect_response)`` when the buffer is
        valid contract JSON, else None (caller falls back to its lenient
        parser). This is the authoritative result — the streamed pieces are a
        preview of exactly this ``text``. ``strict=False`` accepts raw control
        chars inside strings (a literal newline in the text — routine from
        prompt-tier local models; the streaming scanner passes them through, so
        the authoritative parse must agree or history gets poisoned with the
        raw blob)."""
        try:
            obj = json.loads(self.buf, strict=False)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(obj, dict) or "text" not in obj:
            return None
        return (
            str(obj.get("text") or ""),
            str(obj.get("voice_prompt") or ""),
            bool(obj.get("expect_response", False)),
        )
