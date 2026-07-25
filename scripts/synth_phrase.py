#!/usr/bin/env python3
"""Synthesize a canned spoken phrase using Kenzy's OWN TTS service.

Pre-recorded cues (the offline error apology, the "On it." backchannel, the
intercom chimes, …) must be generated ahead of time — TTS may be down when they
play — yet must still sound like the live voice. So this goes through the running
kenzy-tts `/speak` endpoint, which means the cue is produced by EXACTLY the
configured provider and voice (OpenAI `sage`, Kokoro `af_heart`, whatever
`tts.yaml` says) — no hardcoded provider, and no OpenAI key needed for a local
install. The style/tone is passed as the `voice_prompt` (OpenAI honors it;
Kokoro ignores it).

Requires a running kenzy-tts (`python scripts/dev_stack.py --only tts`, or the
full stack). Run from the repo root:

    python scripts/synth_phrase.py         # default error apology → sounds/error.wav
    python scripts/synth_phrase.py --text "On it." -o src/kenzy/node/sounds/thinking.wav
    python scripts/synth_phrase.py --url https://tts-host:8769   # a non-local service

Output is a canonical 24 kHz mono 16-bit WAV (what /speak returns), matching the
node's sound loader and the server's tone format.
"""

from __future__ import annotations

import argparse
import os
import wave
from pathlib import Path

DEFAULT_TEXT = "I'm sorry, but I'm having trouble processing your request at the moment."
DEFAULT_INSTRUCTIONS = (
    "Speak calmly and apologetically, at a measured pace — a brief, sincere "
    "apology from a home voice assistant that cannot complete a request."
)
ROOT = Path(__file__).resolve().parent.parent  # repo root (this script lives in scripts/)
# Try https first (the dev stack terminates TLS on 8769); fall back to plaintext.
DEFAULT_URLS = ["https://127.0.0.1:8769", "http://127.0.0.1:8769"]


def _speak(url: str, text: str, voice_prompt: str) -> tuple[bytes, int, int]:
    """POST to kenzy-tts /speak; return (pcm, sample_rate, channels)."""
    import httpx

    headers: dict[str, str] = {}
    # Sign the request iff the mesh is token-gated (dev mesh is tokenless).
    token = os.environ.get("KENZY_SERVER_TOKEN") or os.environ.get("KENZY_SERVICE_TOKEN")
    if token:
        from kenzy import serviceauth

        headers[serviceauth.SIG_HEADER] = serviceauth.sign_service_request(token, "POST", "/speak")
    resp = httpx.post(
        url.rstrip("/") + "/speak",
        json={"text": text, "voice_prompt": voice_prompt, "room_id": "synth"},
        headers=headers,
        timeout=120.0,
        verify=False,  # self-signed LAN cert (encrypted-but-unverified, Kenzy's posture)
    )
    resp.raise_for_status()
    rate = int(resp.headers.get("X-Sample-Rate", 24000))
    channels = int(resp.headers.get("X-Channels", 1))
    return resp.content, rate, channels


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    ap.add_argument("--text", default=DEFAULT_TEXT)
    ap.add_argument("-o", "--out", default=str(ROOT / "src/kenzy/node/sounds/error.wav"))
    ap.add_argument(
        "--instructions",
        default=DEFAULT_INSTRUCTIONS,
        help="voice_prompt / style — honored by OpenAI, ignored by Kokoro",
    )
    ap.add_argument(
        "--url", default=None, help="kenzy-tts base URL (default: 127.0.0.1:8769, https then http)"
    )
    args = ap.parse_args()

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    load_dotenv(Path.home() / ".config" / "kenzy" / ".env")

    import httpx

    urls = [args.url] if args.url else DEFAULT_URLS
    pcm: bytes | None = None
    rate = channels = 0
    last_err: Exception | None = None
    for url in urls:
        try:
            pcm, rate, channels = _speak(url, args.text, args.instructions)
            print(f"synthesized via {url}")
            break
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError) as exc:
            last_err = exc  # unreachable / wrong scheme — try the next candidate
    if pcm is None:
        raise SystemExit(f"could not reach kenzy-tts — is it running? ({last_err})")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)  # /speak returns int16 PCM
        w.setframerate(rate)
        w.writeframes(pcm)

    with wave.open(str(out)) as w:
        dur = w.getnframes() / w.getframerate()
        print(
            f"{out}  {w.getnchannels()} ch  {w.getframerate()} Hz  "
            f"{w.getsampwidth() * 8}-bit  {dur:.2f} s"
        )


if __name__ == "__main__":
    main()
