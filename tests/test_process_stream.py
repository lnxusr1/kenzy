"""4.4 streaming /process: ndjson event order (head → deltas → end), buffered
bail for non-contract replies, tool events through the loop, and the
placeholder holdback."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from kenzy.llm import llm as llm_app
from kenzy.llm import memory
from kenzy.llm import skills as sk


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "_store", memory.MemoryStore(tmp_path / "facts.jsonl"))

    async def no_fast(utterance, room_id, speaker):
        return None

    monkeypatch.setattr(sk, "dispatch_fast", no_fast)
    yield


def _chunk(content=None, tool_calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=tool_calls))]
    )


def _script_acompletion(monkeypatch, scripts: list[list]):
    """Each script entry is a list of chunks for one streamed model call."""
    calls: list[dict] = []

    async def fake(kwargs, fb_state):
        calls.append(kwargs)
        assert kwargs.get("stream") is True
        batch = scripts.pop(0)

        async def gen():
            for c in batch:
                yield c

        return gen()

    monkeypatch.setattr(sk, "acompletion_with_fallback", fake)
    return calls


def _stream_events(payload: dict) -> list[dict]:
    with TestClient(llm_app.app) as c:
        with c.stream("POST", "/process/stream", json=payload) as r:
            return [json.loads(line) for line in r.iter_lines() if line]


REQ = {"text": "why is the sky blue", "room_id": "office", "session_id": "s1"}


def test_contract_reply_streams_head_deltas_end(monkeypatch):
    raw = (
        '{"voice_prompt": "Calm.", "expect_response": false, '
        '"text": "Blue light scatters. More at sunset."}'
    )
    pieces = [raw[i : i + 7] for i in range(0, len(raw), 7)]
    _script_acompletion(monkeypatch, [[_chunk(content=p) for p in pieces]])

    events = _stream_events(REQ)
    kinds = [e["event"] for e in events]
    assert kinds[0] == "head"
    assert kinds[-1] == "end"
    assert "delta" in kinds
    assert events[0]["voice_prompt"] == "Calm."
    streamed = "".join(e["text"] for e in events if e["event"] == "delta")
    end = events[-1]
    assert end["text"] == "Blue light scatters. More at sunset."
    assert end["text"].startswith(streamed)  # deltas are a prefix preview
    assert end["fast"] is False
    assert end["voice_prompt"] == "Calm."


def test_plain_prose_bails_to_buffered_end(monkeypatch):
    _script_acompletion(
        monkeypatch, [[_chunk(content="The li"), _chunk(content="ghts are on.")]]
    )
    events = _stream_events(REQ)
    kinds = [e["event"] for e in events]
    assert "delta" not in kinds  # bailed — nothing previewed
    assert events[-1]["event"] == "end"
    assert events[-1]["text"] == "The lights are on."  # lenient parse owns it


def test_tool_call_then_reply_emits_tool_event(monkeypatch):
    import litellm

    tc = SimpleNamespace(id="t1", function=SimpleNamespace(name="get_weather", arguments="{}"))
    tool_chunks = [_chunk(tool_calls=[SimpleNamespace(id="t1")])]
    raw = '{"voice_prompt": "Bright.", "expect_response": false, "text": "It is sunny."}'
    _script_acompletion(monkeypatch, [tool_chunks, [_chunk(content=raw)]])
    monkeypatch.setattr(
        litellm,
        "stream_chunk_builder",
        lambda chunks: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[tc]))]
        ),
    )

    async def fake_execute(name, args):
        assert name == "get_weather"
        return "sunny"

    monkeypatch.setattr(sk, "execute", fake_execute)

    events = _stream_events(REQ)
    kinds = [e["event"] for e in events]
    assert "tool" in kinds
    assert kinds.index("tool") < kinds.index("head")
    assert events[-1]["text"] == "It is sunny."


def test_placeholder_never_streams(monkeypatch):
    raw = (
        '{"voice_prompt": "Even.", "expect_response": false, '
        '"text": "The door code is [[lockbox:door-code]] — got it."}'
    )
    pieces = [raw[i : i + 5] for i in range(0, len(raw), 5)]
    _script_acompletion(monkeypatch, [[_chunk(content=p) for p in pieces]])

    events = _stream_events(REQ)
    streamed = "".join(e["text"] for e in events if e["event"] == "delta")
    assert "[[" not in streamed
    assert streamed == "The door code is "  # held from the placeholder on
    assert events[-1]["event"] == "end"


def test_buffered_process_unchanged(monkeypatch):
    # The plain /process endpoint must not stream (sink=None) and must return
    # the same contract fields as before.
    raw = '{"voice_prompt": "Flat.", "expect_response": false, "text": "Paris."}'

    async def fake(kwargs, fb_state):
        assert "stream" not in kwargs
        msg = SimpleNamespace(content=raw, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    monkeypatch.setattr(sk, "acompletion_with_fallback", fake)
    with TestClient(llm_app.app) as c:
        r = c.post("/process", json=REQ)
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "Paris."
    assert body["voice_prompt"] == "Flat."


def test_lockbox_touched_suppresses_all_deltas(monkeypatch):
    # A lockbox exchange must never preview — even though the reply streams
    # llm-side, nothing leaves as a delta; the end event carries everything.
    raw = '{"voice_prompt": "Even.", "expect_response": false, "text": "It is stored."}'
    _script_acompletion(monkeypatch, [[_chunk(content=p) for p in (raw[:20], raw[20:])]])
    monkeypatch.setattr(memory, "lockbox_touched", lambda: True)

    events = _stream_events(REQ)
    assert [e["event"] for e in events if e["event"] == "delta"] == []
    assert events[-1]["event"] == "end"
    assert events[-1]["text"] == "It is stored."


def test_midstream_failure_after_deltas_keeps_spoken_text(monkeypatch):
    # The stream dies after previews went out: what streamed IS the reply of
    # record — no re-call (tools must not re-execute), no broken pipe.
    raw = '{"voice_prompt": "Calm.", "expect_response": false, "text": "First part. Second'

    async def fake(kwargs, fb_state):
        assert kwargs.get("stream") is True

        async def gen():
            yield _chunk(content=raw)
            raise RuntimeError("provider hiccup")

        return gen()

    monkeypatch.setattr(sk, "acompletion_with_fallback", fake)
    events = _stream_events(REQ)
    streamed = "".join(e["text"] for e in events if e["event"] == "delta")
    assert streamed == "First part. Second"  # everything extracted before the break
    end = events[-1]
    assert end["event"] == "end"
    assert end["text"] == "First part. Second"  # what streamed IS the record
    assert end["voice_prompt"] == "Calm."


def test_midstream_failure_before_output_recalls_buffered(monkeypatch):
    # Nothing streamed yet: safe to re-call — but with tool_choice="none" so
    # already-executed tools can never run twice.
    calls: list[dict] = []
    raw = '{"voice_prompt": "Flat.", "expect_response": false, "text": "Recovered."}'

    async def fake(kwargs, fb_state):
        calls.append(kwargs)
        if kwargs.get("stream"):

            async def gen():
                raise RuntimeError("dead on arrival")
                yield  # pragma: no cover

            return gen()
        msg = SimpleNamespace(content=raw, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    monkeypatch.setattr(sk, "acompletion_with_fallback", fake)
    events = _stream_events(REQ)
    assert events[-1]["event"] == "end"
    assert events[-1]["text"] == "Recovered."
    assert len(calls) == 2
    assert calls[1].get("tool_choice") == "none"
    assert "stream" not in calls[1]
