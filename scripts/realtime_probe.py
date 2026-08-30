#!/usr/bin/env python3
"""Realtime-seam probe — the first v6 prototype artifact.

Exercises a real full-S2S engine (OpenAI Realtime API over WebSocket) against
the seam questions in kenzy-design/app/s2s-design.md, and prints a timestamped
event timeline so the answers are measured, not assumed:

  ordering   Does the INPUT transcript land before the response starts?
             (transcript-before-action is a hard seam invariant; OpenAI
             transcribes input asynchronously, so this is the race to measure.)
  cancel     How fast does response.cancel actually stop the delta stream?
             (The fast-path interceptor depends on cancel semantics.)
  tool       Do function_call events ever precede the input transcript?
             (The action gate must hold tools until the transcript is on
             record — measure whether the engine would race it.)

Kenzy-side turn policy is exercised throughout: turn_detection is DISABLED and
the probe commits the audio buffer itself (decision 1: Kenzy owns endpointing;
frames stream in, Kenzy commits the turn).

Usage:
    python scripts/realtime_probe.py ordering [--clip WAV]
    python scripts/realtime_probe.py cancel   [--clip WAV]
    python scripts/realtime_probe.py tool     [--clip WAV with a lighting command]
    python scripts/realtime_probe.py say --text "Turn on the kitchen light." --out /tmp/cmd.wav

Reads OPENAI_API_KEY from the environment or ~/.config/kenzy/.env. Never
prints the key. Each run costs cents (short clips, one-sentence replies);
usage tokens are printed from response.done.
"""

from __future__ import annotations

import argparse
import asyncio
import audioop  # noqa: deprecated in 3.13; lab boxes run 3.11/3.12
import base64
import json
import os
import sys
import time
import wave
from pathlib import Path

DEFAULT_CLIP = Path.home() / ".cache/kenzy-voice-probe/am_adam_1218d4b9ff935ad1.wav"
DEFAULT_MODEL = "gpt-realtime"


def load_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        env = Path.home() / ".config/kenzy/.env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("OPENAI_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip("\"'")
                    break
    if not key:
        sys.exit("OPENAI_API_KEY not found (env or ~/.config/kenzy/.env)")
    return key


def wav_to_pcm24k(path: Path) -> bytes:
    """Any mono/stereo 16-bit WAV -> 24 kHz mono pcm16 (Realtime's native format)."""
    with wave.open(str(path), "rb") as w:
        nch, sw, fr = w.getnchannels(), w.getsampwidth(), w.getframerate()
        pcm = w.readframes(w.getnframes())
    if sw != 2:
        pcm = audioop.lin2lin(pcm, sw, 2)
    if nch == 2:
        pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
    if fr != 24000:
        pcm, _ = audioop.ratecv(pcm, 2, 1, fr, 24000, None)
    return pcm


def say(text: str, out: Path, key: str) -> None:
    """Generate a test clip via OpenAI TTS (self-contained clip factory)."""
    import httpx

    r = httpx.post(
        "https://api.openai.com/v1/audio/speech",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "tts-1", "voice": "onyx", "input": text, "response_format": "wav"},
        timeout=60,
    )
    r.raise_for_status()
    out.write_bytes(r.content)
    print(f"wrote {out} ({len(r.content)} bytes)")


TOOLS = [
    {
        "type": "function",
        "name": "set_light",
        "description": "Turn a light on or off in a room.",
        "parameters": {
            "type": "object",
            "properties": {"room": {"type": "string"}, "on": {"type": "boolean"}},
            "required": ["room", "on"],
        },
    }
]


def padded_tools(n: int) -> list[dict]:
    """N extra plausible smart-home tools, to approximate Kenzy's real schema load."""
    extras = []
    for i in range(n):
        extras.append(
            {
                "type": "function",
                "name": f"device_action_{i}",
                "description": f"Control smart-home device group {i}: query or change its state, "
                "with optional room scoping and a numeric level where the device supports one.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "room": {"type": "string", "description": "Room name"},
                        "action": {"type": "string", "enum": ["on", "off", "toggle", "status"]},
                        "level": {"type": "number", "description": "0-100 where supported"},
                    },
                    "required": ["action"],
                },
            }
        )
    return extras


async def probe(mode: str, clip: Path, model: str, voice: str, key: str,
                extra_tools: int = 0, pad_instructions: int = 0, paced: bool = False,
                url_base: str = "", auto_response: bool = False) -> None:
    import websockets

    base = url_base or "wss://api.openai.com/v1/realtime"
    url = f"{base}?model={model}"
    headers = {"Authorization": f"Bearer {key}"}  # GA API: no beta header; local servers ignore it
    pcm = wav_to_pcm24k(clip)
    print(f"clip: {clip.name}  ({len(pcm)} bytes pcm16@24k = {len(pcm)/48000:.2f}s)")
    print(f"model: {model}  mode: {mode}  turn_detection: DISABLED (probe commits)\n")

    async with websockets.connect(url, additional_headers=headers, max_size=1 << 24) as ws:
        t0 = time.monotonic()

        def ms() -> float:
            return (time.monotonic() - t0) * 1000.0

        def row(tag: str, detail: str = "") -> None:
            print(f"  +{ms():8.0f}ms  {tag}  {detail}".rstrip())

        session: dict = {  # GA shape (gpt-realtime, post-Aug-2025)
            "type": "realtime",
            "output_modalities": ["audio"],
            "instructions": "You are a voice assistant. Reply in one short sentence.",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "turn_detection": None,  # decision 1: Kenzy owns the turn boundary
                    "transcription": {"model": "whisper-1"},
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "voice": voice,
                },
            },
        }
        if mode == "tool":
            session["tools"] = TOOLS + padded_tools(extra_tools)
            session["tool_choice"] = "auto"
        if pad_instructions:
            filler = (
                "House context: the home has many rooms, each with devices, scenes, and "
                "schedules; prefer scoped actions, confirm consequential ones, keep replies terse. "
            )
            session["instructions"] += " " + (filler * (pad_instructions // len(filler) + 1))[:pad_instructions]
        await ws.send(json.dumps({"type": "session.update", "session": session}))

        # stream the clip in 40 ms chunks (what a node's frames look like),
        # then commit + request the response — the Kenzy turn policy. Paced
        # mode sleeps between chunks to simulate real-time capture, so the
        # server holds the audio for the utterance duration before commit.
        chunk = 1920  # 40 ms @ 24 kHz mono pcm16
        for i in range(0, len(pcm), chunk):
            await ws.send(
                json.dumps(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(pcm[i : i + chunk]).decode(),
                    }
                )
            )
            if paced:
                await asyncio.sleep(0.04)
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        t_commit = ms()
        if auto_response:
            # STT-driven engines (HF s2s local server) auto-respond after the
            # transcript; a commit-time response.create is auto-cancelled there.
            row("COMMIT sent (auto-response engine — no response.create)")
        else:
            await ws.send(json.dumps({"type": "response.create"}))
            row("COMMIT + response.create sent")

        first_audio = first_transcript_evt = first_tool_evt = None
        cancel_sent_at = None
        deltas_after_cancel = 0
        audio_deltas = 0
        audio_bytes = 0
        out_transcript: list[str] = []
        deadline = time.monotonic() + 60

        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
            except TimeoutError:
                row("(recv timeout — ending)")
                break
            evt = json.loads(raw)
            et = evt.get("type", "?")

            if et in ("response.audio.delta", "response.output_audio.delta"):
                audio_deltas += 1
                audio_bytes += len(base64.b64decode(evt.get("delta", "")))
                if cancel_sent_at is not None:
                    deltas_after_cancel += 1
                if first_audio is None:
                    first_audio = ms()
                    row("response.audio.delta  <<< FIRST AUDIO")
                    if mode == "cancel":
                        await ws.send(json.dumps({"type": "response.cancel"}))
                        cancel_sent_at = ms()
                        row("response.cancel SENT")
            elif et in ("response.audio_transcript.delta", "response.output_audio_transcript.delta"):
                out_transcript.append(evt.get("delta", ""))
            elif "input_audio_transcription" in et:
                if first_transcript_evt is None:
                    first_transcript_evt = ms()
                row(et, json.dumps(evt.get("transcript", evt.get("delta", "")))[:80])
            elif et.startswith("response.function_call") or (
                et == "response.output_item.added"
                and evt.get("item", {}).get("type") == "function_call"
            ):
                if first_tool_evt is None:
                    first_tool_evt = ms()
                row(et, json.dumps(evt.get("item", evt.get("delta", "")))[:100])
            elif et in ("response.done", "error", "session.created", "session.updated",
                        "input_audio_buffer.committed", "response.created",
                        "response.output_item.done", "rate_limits.updated"):
                detail = ""
                if et == "error":
                    detail = json.dumps(evt.get("error", {}))[:200]
                if et == "response.done":
                    resp = evt.get("response", {})
                    detail = f"status={resp.get('status')}"
                    usage = resp.get("usage") or {}
                    if usage:
                        detail += f" tokens(in={usage.get('input_tokens')},out={usage.get('output_tokens')})"
                if et == "response.output_item.done":
                    item = evt.get("item", {})
                    if item.get("type") == "function_call":
                        detail = f"function_call {item.get('name')}({item.get('arguments')})"
                row(et, detail)
                if et == "response.done":
                    # linger: late events (async input transcription especially)
                    # arrive AFTER the response is done — that lateness is itself
                    # the measurement.
                    try:
                        while True:
                            raw = await asyncio.wait_for(ws.recv(), timeout=3)
                            late = json.loads(raw)
                            lt = late.get("type", "?")
                            if lt in ("response.audio.delta", "response.output_audio.delta"):
                                deltas_after_cancel += 1
                                continue
                            if "input_audio_transcription" in lt:
                                if first_transcript_evt is None:
                                    first_transcript_evt = ms()
                                row(lt + "  (LATE — after response.done)",
                                    json.dumps(late.get("transcript", late.get("delta", "")))[:80])
                    except TimeoutError:
                        pass
                    break

        print("\n=== summary ===")
        print(f"  commit -> first audio : {first_audio - t_commit:8.0f} ms" if first_audio else "  no audio")
        if first_transcript_evt:
            print(f"  commit -> input transcript event: {first_transcript_evt - t_commit:8.0f} ms")
            if first_audio:
                gap = first_transcript_evt - first_audio
                print(
                    f"  transcript vs first audio: transcript {'LAGGED by ' + format(gap, '.0f') + ' ms — THE RACE IS REAL' if gap > 0 else 'led by ' + format(-gap, '.0f') + ' ms'}"
                )
        else:
            print("  input transcript event: NEVER ARRIVED")
        if mode == "tool" and first_tool_evt:
            if first_transcript_evt:
                gap = first_tool_evt - first_transcript_evt
                lead = "AFTER" if gap > 0 else "BEFORE"
                print(f"  first tool event {lead} input transcript by {abs(gap):.0f} ms")
            else:
                print("  tool event arrived; input transcript never did")
        if cancel_sent_at is not None:
            print(f"  audio deltas after cancel: {deltas_after_cancel}")
        print(f"  audio: {audio_deltas} deltas, {audio_bytes} bytes ({audio_bytes/48000:.2f}s @24k)")
        if out_transcript:
            print(f"  reply transcript: {''.join(out_transcript)[:120]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["ordering", "cancel", "tool", "say"])
    ap.add_argument("--clip", type=Path, default=DEFAULT_CLIP)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--voice", default="marin")
    ap.add_argument("--text", default="Turn on the kitchen light.")
    ap.add_argument("--out", type=Path, default=Path("/tmp/realtime_probe_cmd.wav"))
    ap.add_argument("--extra-tools", type=int, default=0, help="pad with N dummy tool schemas")
    ap.add_argument("--pad-instructions", type=int, default=0, help="pad instructions to ~N chars")
    ap.add_argument("--paced", action="store_true", help="stream chunks at real-time rate before commit")
    ap.add_argument("--url", default="", help="override endpoint, e.g. ws://127.0.0.1:8765/v1/realtime (local HF s2s server)")
    ap.add_argument("--auto-response", action="store_true", help="engine auto-responds after STT; skip response.create")
    args = ap.parse_args()
    key = load_key()
    if args.mode == "say":
        say(args.text, args.out, key)
        return 0
    asyncio.run(probe(args.mode, args.clip, args.model, args.voice, key,
                      args.extra_tools, args.pad_instructions, args.paced, args.url,
                      args.auto_response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
