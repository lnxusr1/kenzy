"""Stage adapters — wiring the engine's injected seams to the real backends.

Spec: kenzy-design/app/s2s-design.md, decision 8 (as clarified 2026-08-26):

- **transcribe** -> kenzy-stt's ``POST /transcribe`` door (the same contract
  the classic pipeline's server uses — one service, one Whisper in VRAM).
- **synthesize** -> kenzy-tts's ``POST /speak`` door (24 kHz mono int16 PCM).
  The voice identity is the TTS service's *configured* voice (seam decision
  5 — one configured voice identity); the session's voice field is advisory
  on this stage.
- **generate** -> the model provider DIRECTLY (OpenAI-compatible
  chat-completions streaming, tools passed through, calls emitted, never
  executed). kenzy-llm is not in this path: it is the skill loop, not the
  model — the weights live in the provider, so "models load once" is
  satisfied there.

Service-to-service calls carry the token-proof signature (``X-Kenzy-Auth``)
exactly as every other backend's outbound calls do; the provider call carries
its own bearer key, read from the environment variable the config names (the
``endpoint_kwargs`` discipline: OPENAI_API_KEY never goes to a custom
base_url).
"""

from __future__ import annotations

import base64
import json
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from kenzy import serviceauth, tlsutil
from kenzy.s2s.server import (
    Generate,
    GenEvent,
    GenRequest,
    GenText,
    GenToolCall,
    Synthesize,
    Transcribe,
)

log = logging.getLogger(__name__)

#: 100 ms of 24 kHz mono int16 per audio chunk toward the session.
_PCM_CHUNK = 4800


def _signed(method: str, url: str) -> dict[str, str]:
    """The token-proof outbound signature every backend call carries."""
    token = serviceauth.service_token_from_env()
    if not token:
        return {}
    path = urlparse(url).path or "/"
    return {serviceauth.SIG_HEADER: serviceauth.sign_service_request(token, method, path)}


def http_transcribe(
    url: str, *, timeout: float = 30.0, transport: httpx.AsyncBaseTransport | None = None
) -> Transcribe:
    """kenzy-stt's whole-utterance door (open question 1's chunked variant
    slots in behind the same ``Transcribe`` signature later)."""

    async def transcribe(pcm: bytes) -> str:
        payload = {
            "audio_b64": base64.b64encode(pcm).decode(),
            "room_id": "s2s",
            "session_id": None,
        }
        async with httpx.AsyncClient(verify=tlsutil.httpx_verify(), transport=transport) as client:
            resp = await client.post(
                url, json=payload, timeout=timeout, headers=_signed("POST", url)
            )
            resp.raise_for_status()
        return str(resp.json()["text"])

    return transcribe


def http_synthesize(
    url: str, *, timeout: float = 30.0, transport: httpx.AsyncBaseTransport | None = None
) -> Synthesize:
    """kenzy-tts's ``/speak`` door, re-chunked for the session's delta stream."""

    async def synthesize(text: str, _voice: str) -> AsyncIterator[bytes]:
        payload = {"text": text, "voice_prompt": "", "room_id": "s2s", "sensitive": False}
        async with httpx.AsyncClient(verify=tlsutil.httpx_verify(), transport=transport) as client:
            resp = await client.post(
                url, json=payload, timeout=timeout, headers=_signed("POST", url)
            )
            resp.raise_for_status()
            pcm = resp.content
        for i in range(0, len(pcm), _PCM_CHUNK):
            yield pcm[i : i + _PCM_CHUNK]

    return synthesize


# ------------------------------------------------------------------ generation


@dataclass(frozen=True)
class ProviderConfig:
    """The generation provider — OpenAI-compatible, deliberately independent of
    the classic pipeline's model choice."""

    base_url: str = ""  # "" = api.openai.com; local vLLM/proxy otherwise
    model: str = "gpt-5.1"
    #: Env var holding the API key. The CONFIG key is ``auth_env`` — never a
    #: name containing key/token/secret, or the server's secret filter strips
    #: it from the served config (the 5.0.4 trap, met again live 2026-08-26).
    auth_env: str = "OPENAI_API_KEY"
    #: None = the provider's default (OpenAI's newer model families REJECT any
    #: explicit temperature — measured live 2026-08-26). Set 0.0 explicitly for
    #: deterministic local serving (vLLM et al.).
    temperature: float | None = None
    #: Reply-length ceiling per turn, in tokens (config key ``max_output`` —
    #: the secret-filter rename; the wire field is ``max_completion_tokens``,
    #: the modern name — ``max_tokens`` is refused by the same model families).
    max_output: int = 512
    timeout: float = 60.0

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> ProviderConfig:
        raw_temp = cfg.get("temperature")
        return cls(
            base_url=str(cfg.get("base_url", "") or ""),
            model=str(cfg.get("model", cls.model)),
            auth_env=str(cfg.get("auth_env", cls.auth_env)),
            temperature=float(raw_temp) if raw_temp is not None else None,
            max_output=int(cfg.get("max_output", cls.max_output)),
            timeout=float(cfg.get("timeout", cls.timeout)),
        )


def _chat_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Realtime session tools are flat; chat completions nests them."""
    if "function" in tool:
        return tool
    return {
        "type": "function",
        "function": {
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
        },
    }


def _chat_messages(req: GenRequest) -> list[dict[str, Any]]:
    """The session's one history, translated to chat-completions shape.

    Call/output ADJACENCY is normalized here: chat completions requires a
    ``tool`` message to immediately follow the assistant message carrying its
    ``tool_calls``, but the session history interleaves (the reply TEXT is
    recorded after the loop, so speak-while-calling — the working-pace
    acknowledgment — lands assistant text between a call and its output).
    Found live 2026-08-30, the first time a model both spoke and called in
    one response. Outputs are paired to their calls here; an unmatched call
    or orphaned output is dropped rather than shipped as a guaranteed 400.
    """
    messages: list[dict[str, Any]] = []
    if req.instructions:
        messages.append({"role": "system", "content": req.instructions})
    outputs = {
        str(item.get("call_id", "")): str(item.get("output", ""))
        for item in req.history
        if item.get("type") == "function_call_output"
    }
    for item in req.history:
        kind = item.get("type")
        if kind == "function_call":
            call_id = str(item.get("call_id", ""))
            if call_id not in outputs:
                continue  # in-flight or abandoned call: never ship half a pair
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": str(item.get("name", "")),
                                "arguments": str(item.get("arguments", "{}")),
                            },
                        }
                    ],
                }
            )
            messages.append(
                {"role": "tool", "tool_call_id": call_id, "content": outputs[call_id]}
            )
        elif kind == "function_call_output":
            continue  # emitted beside its call above
        elif kind == "message" and isinstance(item.get("content"), list):
            # A Realtime-shaped message item (conversation.item.create) — the
            # form the seam's add_context sends, valid against OpenAI's API
            # too, so ONE client shape serves both engines. Flatten the
            # content parts to their text.
            text = " ".join(
                str(part.get("text", ""))
                for part in item["content"]
                if isinstance(part, dict) and part.get("text")
            ).strip()
            if text:
                messages.append({"role": str(item.get("role", "system")), "content": text})
        elif "role" in item:
            messages.append({"role": str(item["role"]), "content": str(item.get("content", ""))})
    return messages


def provider_generate(
    cfg: ProviderConfig, *, transport: httpx.AsyncBaseTransport | None = None
) -> Generate:
    """Streamed chat-completions against the provider: text deltas yield as
    they arrive; tool calls accumulate across deltas and yield complete (the
    session emits them as finished ``function_call`` items — never partial)."""

    async def generate(req: GenRequest) -> AsyncIterator[GenEvent]:
        body: dict[str, Any] = {
            "model": cfg.model,
            "messages": _chat_messages(req),
            "stream": True,
            "max_completion_tokens": cfg.max_output,
        }
        if cfg.temperature is not None:
            body["temperature"] = cfg.temperature
        if req.tools:
            body["tools"] = [_chat_tool(t) for t in req.tools]
            body["tool_choice"] = "auto"
        api_key = os.environ.get(cfg.auth_env, "")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        base = (cfg.base_url or "https://api.openai.com/v1").rstrip("/")
        calls: dict[int, dict[str, str]] = {}
        async with (
            httpx.AsyncClient(transport=transport) as client,
            client.stream(
                "POST",
                f"{base}/chat/completions",
                json=body,
                headers=headers,
                timeout=cfg.timeout,
            ) as resp,
        ):
            if resp.status_code >= 400:
                # The provider's own explanation is the diagnosis (lived
                # 2026-08-30: a bare '400 Bad Request' hid a tool-ordering
                # rejection behind two more debugging steps).
                detail = (await resp.aread()).decode(errors="replace")[:500]
                raise RuntimeError(
                    f"provider {resp.status_code} from {base}/chat/completions: {detail}"
                )
            done = False
            async for raw in resp.aiter_lines():
                if not raw.startswith("data:"):
                    continue
                data = raw[5:].strip()
                if data == "[DONE]":
                    break
                evt = json.loads(data)
                for choice in evt.get("choices", []):
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if content:
                        yield GenText(str(content))
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(
                            int(tc.get("index") or 0), {"id": "", "name": "", "args": ""}
                        )
                        if tc.get("id"):
                            slot["id"] = str(tc["id"])
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += str(fn["name"])
                        if fn.get("arguments"):
                            slot["args"] += str(fn["arguments"])
                    # Terminate on the model's OWN completion signal, not only
                    # on the "[DONE]" sentinel: some providers/models (measured
                    # live 2026-08-29: gpt-5.1) send finish_reason and then keep
                    # the connection open WITHOUT "[DONE]", so waiting only for
                    # the sentinel blocks aiter_lines until the read timeout —
                    # a deterministic ~30 s stall that errored every delivery
                    # turn. finish_reason is the standard end-of-message signal.
                    if choice.get("finish_reason"):
                        done = True
                if done:
                    break
        for index in sorted(calls):
            slot = calls[index]
            yield GenToolCall(
                call_id=slot["id"] or f"call_{index}",
                name=slot["name"],
                arguments_json=slot["args"] or "{}",
            )

    return generate


__all__ = [
    "ProviderConfig",
    "http_synthesize",
    "http_transcribe",
    "provider_generate",
]
