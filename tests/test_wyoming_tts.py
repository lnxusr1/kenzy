"""Tests for the Wyoming TTS listener (F3.3): describe/synthesize round-trip
over a real TCP socket, config gating, and error behavior."""

from __future__ import annotations

import asyncio
import socket

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.error import Error
from wyoming.info import Describe, Info
from wyoming.server import AsyncServer
from wyoming.tts import Synthesize

from kenzy.tts import wyoming_server as wy


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _fake_synthesise(text: str, voice_prompt: str) -> bytes:
    # 100 int16 samples of "audio" derived from the text length.
    return bytes(2 * 100) if text != "boom" else (_ for _ in ()).throw(RuntimeError("no voice"))


async def _serve(port: int):
    server = AsyncServer.from_uri(f"tcp://127.0.0.1:{port}")
    task = asyncio.create_task(server.run(wy._handler_factory(_fake_synthesise, "sage")))
    await asyncio.sleep(0.1)  # listener up
    return server, task


async def test_describe_reports_kenzy_voice():
    port = _free_port()
    server, task = await _serve(port)
    try:
        async with AsyncTcpClient("127.0.0.1", port) as client:
            await client.write_event(Describe().event())
            event = await asyncio.wait_for(client.read_event(), timeout=5)
        info = Info.from_event(event)
        assert info.tts and info.tts[0].name == "kenzy"
        assert info.tts[0].voices[0].name == "sage"
        assert info.tts[0].voices[0].installed
    finally:
        task.cancel()


async def test_synthesize_streams_pcm():
    port = _free_port()
    server, task = await _serve(port)
    try:
        async with AsyncTcpClient("127.0.0.1", port) as client:
            await client.write_event(Synthesize(text="hello world").event())
            start = await asyncio.wait_for(client.read_event(), timeout=5)
            assert AudioStart.is_type(start.type)
            meta = AudioStart.from_event(start)
            assert (meta.rate, meta.width, meta.channels) == (24000, 2, 1)
            audio = b""
            while True:
                event = await asyncio.wait_for(client.read_event(), timeout=5)
                if AudioStop.is_type(event.type):
                    break
                assert AudioChunk.is_type(event.type)
                audio += AudioChunk.from_event(event).audio
        assert len(audio) == 200  # the fake's 100 int16 samples
    finally:
        task.cancel()


async def test_synthesis_failure_reports_error_event():
    port = _free_port()
    server, task = await _serve(port)
    try:
        async with AsyncTcpClient("127.0.0.1", port) as client:
            await client.write_event(Synthesize(text="boom").event())
            event = await asyncio.wait_for(client.read_event(), timeout=5)
            assert Error.is_type(event.type)
            # The connection survives a failed request.
            await client.write_event(Synthesize(text="ok now").event())
            event = await asyncio.wait_for(client.read_event(), timeout=5)
            assert AudioStart.is_type(event.type)
    finally:
        task.cancel()


def test_install_is_noop_when_disabled():
    class _App:
        class router:
            on_startup: list = []
            on_shutdown: list = []

    wy.install_wyoming_tts(
        _App, {}, _fake_synthesise, voice_name="sage", bind="127.0.0.1"
    )
    wy.install_wyoming_tts(
        _App,
        {"wyoming": {"enabled": False}},
        _fake_synthesise,
        voice_name="sage",
        bind="127.0.0.1",
    )
    assert _App.router.on_startup == [] and _App.router.on_shutdown == []


def test_install_registers_hooks_when_enabled():
    class _App:
        class router:
            on_startup: list = []
            on_shutdown: list = []

    wy.install_wyoming_tts(
        _App,
        {"wyoming": {"enabled": True, "port": _free_port()}},
        _fake_synthesise,
        voice_name="sage",
        bind="127.0.0.1",
    )
    assert len(_App.router.on_startup) == 1 and len(_App.router.on_shutdown) == 1
