"""Stage-adapter tests — the HTTP contracts pinned against recorded requests
(kenzy-stt /transcribe, kenzy-tts /speak, the provider's chat-completions
stream with tools). No network: httpx.MockTransport plays every backend."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from kenzy.s2s.server import GenRequest, GenText, GenToolCall
from kenzy.s2s.stages import (
    ProviderConfig,
    http_synthesize,
    http_transcribe,
    provider_generate,
)


def _capture(response: httpx.Response, seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response

    return httpx.MockTransport(handler)


async def test_transcribe_speaks_the_stt_contract() -> None:
    seen: list[httpx.Request] = []
    transport = _capture(httpx.Response(200, json={"text": "turn on the light"}), seen)
    transcribe = http_transcribe("http://stt.local:8767/transcribe", transport=transport)
    assert await transcribe(b"\x00\x01" * 160) == "turn on the light"
    body = json.loads(seen[0].content)
    assert set(body) == {"audio_b64", "room_id", "session_id"}  # the server's own contract
    assert body["room_id"] == "s2s"


async def test_synthesize_rechunks_the_speak_response() -> None:
    pcm = b"\x01\x02" * 5000  # 10,000 bytes of 24 kHz int16
    seen: list[httpx.Request] = []
    transport = _capture(httpx.Response(200, content=pcm), seen)
    synthesize = http_synthesize("http://tts.local:8769/speak", transport=transport)
    chunks = [c async for c in synthesize("hello", "af_heart")]
    assert b"".join(chunks) == pcm
    assert [len(c) for c in chunks] == [4800, 4800, 400]  # 100 ms slices + the tail
    body = json.loads(seen[0].content)
    assert body["text"] == "hello" and body["sensitive"] is False


def _sse(*events: dict[str, Any]) -> bytes:
    lines = [f"data: {json.dumps(e)}" for e in events]
    lines.append("data: [DONE]")
    return ("\n".join(lines) + "\n").encode()


async def test_provider_generate_streams_text_and_assembles_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    stream = _sse(
        {"choices": [{"delta": {"content": "On "}}]},
        {"choices": [{"delta": {"content": "it."}}]},
        # tool-call arguments arrive SPLIT across deltas — assembled, never partial
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": "set_light", "arguments": '{"on"'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ": true}"}}]}}
            ]
        },
    )
    seen: list[httpx.Request] = []
    transport = _capture(httpx.Response(200, content=stream), seen)
    generate = provider_generate(
        ProviderConfig(base_url="http://vllm.local:8000/v1", model="moe"), transport=transport
    )
    request = GenRequest(
        instructions="be brief",
        tools=[{"type": "function", "name": "set_light", "description": "d", "parameters": {}}],
        history=[
            {"role": "user", "content": "turn on the light"},
            {"type": "function_call", "call_id": "c0", "name": "get_time", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c0", "output": "3pm"},
        ],
    )
    events = [e async for e in generate(request)]
    assert events[:2] == [GenText("On "), GenText("it.")]
    assert events[2] == GenToolCall("call_1", "set_light", '{"on": true}')

    sent = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer sk-test"
    roles = [m.get("role") for m in sent["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]  # one history, translated
    assert sent["messages"][2]["tool_calls"][0]["function"]["name"] == "get_time"
    assert sent["messages"][3] == {"role": "tool", "tool_call_id": "c0", "content": "3pm"}
    # Realtime's flat tool shape arrives nested, as chat completions requires
    assert sent["tools"][0]["function"]["name"] == "set_light"


async def test_provider_generate_without_tools_omits_the_fields() -> None:
    seen: list[httpx.Request] = []
    transport = _capture(
        httpx.Response(200, content=_sse({"choices": [{"delta": {"content": "hi"}}]})), seen
    )
    generate = provider_generate(ProviderConfig(base_url="http://x/v1"), transport=transport)
    events = [e async for e in generate(GenRequest("", [], [{"role": "user", "content": "hey"}]))]
    assert events == [GenText("hi")]
    sent = json.loads(seen[0].content)
    assert "tools" not in sent and "tool_choice" not in sent
    assert [m["role"] for m in sent["messages"]] == ["user"]  # no empty system message


def test_chat_messages_flatten_realtime_message_items() -> None:
    """A Realtime-shaped message item (the seam's add_context — identity and
    other out-of-band context) flattens to an ordinary chat message, so ONE
    client item shape serves our engine and OpenAI's alike."""
    from kenzy.s2s.stages import GenRequest, _chat_messages

    req = GenRequest(
        "be brief",
        [],
        [
            {"role": "user", "content": "hello"},
            {
                "type": "message",
                "role": "system",
                "content": [{"type": "input_text", "text": "The current speaker is Alex."}],
            },
            {"type": "message", "role": "system", "content": []},  # empty ⇒ dropped
        ],
    )
    msgs = _chat_messages(req)
    assert msgs[0] == {"role": "system", "content": "be brief"}
    assert msgs[1] == {"role": "user", "content": "hello"}
    assert msgs[2] == {"role": "system", "content": "The current speaker is Alex."}
    assert len(msgs) == 3


def test_chat_messages_normalize_call_output_adjacency() -> None:
    """The live 2026-08-30 shape: speak-while-calling records the reply text
    BETWEEN a function_call and its output. Chat completions requires the
    pair adjacent — the translation reorders, and never ships half a pair."""
    from kenzy.s2s.stages import GenRequest, _chat_messages

    req = GenRequest(
        "be kenzy",
        [],
        [
            {"role": "user", "content": "search the web"},
            {"type": "function_call", "call_id": "c1", "name": "web_search",
             "arguments": "{}"},
            {"role": "assistant", "content": "I'll look that up now."},
            {"type": "function_call_output", "call_id": "c1", "output": "Started."},
            {"type": "function_call", "call_id": "c-inflight", "name": "x",
             "arguments": "{}"},  # no output yet: must not ship
        ],
    )
    msgs = _chat_messages(req)
    roles = [(m["role"], bool(m.get("tool_calls"))) for m in msgs]
    assert roles == [
        ("system", False),
        ("user", False),
        ("assistant", True),   # the call...
        ("tool", False),       # ...immediately followed by its output
        ("assistant", False),  # the spoken acknowledgment, displaced after
    ]
    assert msgs[3]["tool_call_id"] == "c1"
    assert all("c-inflight" not in str(m) for m in msgs)


async def test_provider_generate_terminates_on_finish_reason_without_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream that sends finish_reason but NO "[DONE]" sentinel (measured
    live 2026-08-29: gpt-5.1 keeps the connection open) must still terminate —
    or aiter_lines blocks until the read timeout, a deterministic stall that
    errored every task delivery turn."""
    from kenzy.s2s.stages import ProviderConfig, provider_generate

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    # Deliberately NO "data: [DONE]" line — just content then finish_reason.
    body = (
        b'data: {"choices": [{"delta": {"content": "Here are the results."}}]}\n'
        b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n'
    )
    seen: list[httpx.Request] = []
    transport = _capture(httpx.Response(200, content=body), seen)
    gen = provider_generate(ProviderConfig(model="gpt-5.1"), transport=transport)

    events = [e async for e in gen(GenRequest("be brief", [], []))]
    # It terminated (didn't hang) and yielded the content.
    assert any(isinstance(e, GenText) and "results" in e.text for e in events)
