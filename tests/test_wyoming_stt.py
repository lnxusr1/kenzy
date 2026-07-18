"""Tests for the Wyoming STT listener (F3.4): describe/transcribe round-trip
over a real TCP socket, audio-format conversion, and error behavior."""

from __future__ import annotations

import asyncio
import socket

import numpy as np
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.error import Error
from wyoming.info import Describe, Info
from wyoming.server import AsyncServer

from kenzy.stt import wyoming_server as wy


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


_seen: list[bytes] = []


async def _fake_transcribe(pcm: bytes) -> str:
    if pcm == b"boom" * 2:
        raise RuntimeError("no ears")
    _seen.append(pcm)
    return f"heard {len(pcm)} bytes"


async def _serve(port: int):
    server = AsyncServer.from_uri(f"tcp://127.0.0.1:{port}")
    task = asyncio.create_task(server.run(wy._handler_factory(_fake_transcribe, "base")))
    await asyncio.sleep(0.1)
    return server, task


async def _send_audio(client, pcm: bytes, rate=16000, width=2, channels=1):
    await client.write_event(Transcribe().event())
    await client.write_event(AudioStart(rate=rate, width=width, channels=channels).event())
    for i in range(0, len(pcm), 1024):
        await client.write_event(
            AudioChunk(audio=pcm[i : i + 1024], rate=rate, width=width, channels=channels).event()
        )
    await client.write_event(AudioStop().event())


async def test_describe_reports_kenzy_model():
    port = _free_port()
    server, task = await _serve(port)
    try:
        async with AsyncTcpClient("127.0.0.1", port) as client:
            await client.write_event(Describe().event())
            event = await asyncio.wait_for(client.read_event(), timeout=5)
        info = Info.from_event(event)
        assert info.asr and info.asr[0].name == "kenzy"
        assert info.asr[0].models[0].name == "base"
    finally:
        task.cancel()


async def test_transcribe_roundtrip_and_reuse():
    port = _free_port()
    server, task = await _serve(port)
    pcm = bytes(3200)  # 100 ms of silence @16k
    try:
        async with AsyncTcpClient("127.0.0.1", port) as client:
            await _send_audio(client, pcm)
            event = await asyncio.wait_for(client.read_event(), timeout=5)
            assert Transcript.is_type(event.type)
            assert Transcript.from_event(event).text == "heard 3200 bytes"
            # Second request on the same connection starts a fresh buffer.
            await _send_audio(client, pcm)
            event = await asyncio.wait_for(client.read_event(), timeout=5)
            assert Transcript.from_event(event).text == "heard 3200 bytes"
    finally:
        task.cancel()


async def test_stereo_and_rate_are_converted():
    port = _free_port()
    server, task = await _serve(port)
    # 48 kHz stereo int16 — must arrive at the transcriber as 16 kHz mono.
    stereo = np.zeros(4800 * 2, dtype=np.int16).tobytes()  # 100 ms
    _seen.clear()
    try:
        async with AsyncTcpClient("127.0.0.1", port) as client:
            await _send_audio(client, stereo, rate=48000, channels=2)
            event = await asyncio.wait_for(client.read_event(), timeout=5)
            assert Transcript.is_type(event.type)
        assert len(_seen[-1]) == 3200  # 100 ms @ 16 kHz mono int16
    finally:
        task.cancel()


async def test_failure_reports_error_and_survives():
    port = _free_port()
    server, task = await _serve(port)
    try:
        async with AsyncTcpClient("127.0.0.1", port) as client:
            await _send_audio(client, b"boom" * 2)
            event = await asyncio.wait_for(client.read_event(), timeout=5)
            assert Error.is_type(event.type)
            await _send_audio(client, bytes(3200))
            event = await asyncio.wait_for(client.read_event(), timeout=5)
            assert Transcript.is_type(event.type)
    finally:
        task.cancel()


def test_to_pipeline_pcm():
    # Identity for the pipeline format.
    pcm = bytes(3200)
    assert wy.to_pipeline_pcm(pcm, 16000, 2, 1) == pcm
    # Stereo mean-mix halves the sample count.
    stereo = np.array([100, 200, 300, 500], dtype=np.int16).tobytes()
    mono = np.frombuffer(wy.to_pipeline_pcm(stereo, 16000, 2, 2), dtype=np.int16)
    assert list(mono) == [150, 400]
    # Resample 48k → 16k thirds the sample count.
    x = np.zeros(4800, dtype=np.int16).tobytes()
    assert len(wy.to_pipeline_pcm(x, 48000, 2, 1)) == 3200
    # Non-16-bit is refused.
    try:
        wy.to_pipeline_pcm(pcm, 16000, 1, 1)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
