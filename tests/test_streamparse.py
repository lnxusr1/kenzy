"""The streaming reply extractor: header-before-text extraction, unescaping
across delta boundaries, bail-to-buffered on non-contract content, and the
streamed-pieces == final-text invariant."""

from __future__ import annotations

import json

from kenzy.llm.streamparse import StreamExtract

REPLY = {
    "voice_prompt": "Calm and warm.",
    "expect_response": False,
    "text": 'She said "hi" — twice.\nThen left. 🎉',
}
RAW = json.dumps(REPLY, ensure_ascii=False)
RAW_ESCAPED = json.dumps(REPLY, ensure_ascii=True)  # \uXXXX for the emoji


def _drive(raw: str, size: int) -> tuple[StreamExtract, str]:
    ex = StreamExtract()
    out = ""
    for i in range(0, len(raw), size):
        out += ex.feed(raw[i : i + size])
    return ex, out


def test_single_feed_extracts_everything():
    ex, out = _drive(RAW, len(RAW))
    assert out == REPLY["text"]
    assert ex.head == {"voice_prompt": "Calm and warm.", "expect_response": False}
    assert ex.done and not ex.bailed
    assert ex.finalize() == (REPLY["text"], "Calm and warm.", False)


def test_every_chunk_size_reassembles_exactly():
    # The invariant the server relies on: streamed pieces concatenate to the
    # final text, whatever the delta boundaries — including mid-key, mid-escape.
    for size in range(1, 24):
        ex, out = _drive(RAW, size)
        assert out == REPLY["text"], f"chunk size {size}"
        assert ex.head is not None, f"chunk size {size}"


def test_unicode_escapes_across_boundaries():
    for size in (1, 2, 3, 5, 7):
        ex, out = _drive(RAW_ESCAPED, size)
        assert out == REPLY["text"], f"chunk size {size}"


def test_expect_response_true_in_head():
    raw = '{"voice_prompt": "Playful.", "expect_response": true, "text": "Knock knock."}'
    ex, out = _drive(raw, 4)
    assert ex.head == {"voice_prompt": "Playful.", "expect_response": True}
    assert out == "Knock knock."


def test_plain_prose_bails_immediately():
    ex = StreamExtract()
    assert ex.feed("The lights are on now.") == ""
    assert ex.bailed
    assert ex.finalize() is None  # caller falls back to _parse_response


def test_markdown_fence_bails():
    ex = StreamExtract()
    ex.feed('```json\n{"text": "hi"}\n```')
    assert ex.bailed


def test_text_first_order_still_streams_without_head():
    # A provider ignoring the schema order: text arrives first — pieces still
    # stream, the header is just unavailable until finalize.
    raw = '{"text": "Hello there.", "voice_prompt": "Flat.", "expect_response": false}'
    ex, out = _drive(raw, 5)
    assert out == "Hello there."
    assert ex.head == {} or ex.head is None  # nothing usable before the text key
    assert ex.finalize() == ("Hello there.", "Flat.", False)


def test_finalize_is_authoritative_on_truncated_stream():
    ex = StreamExtract()
    ex.feed('{"voice_prompt": "x", "expect_response": false, "text": "cut of')
    assert not ex.done
    assert ex.finalize() is None  # invalid JSON — lenient parser's job


def test_leading_whitespace_tolerated():
    raw = '  \n {"voice_prompt": "v", "expect_response": false, "text": "ok"}'
    ex, out = _drive(raw, 3)
    assert out == "ok"
    assert not ex.bailed
