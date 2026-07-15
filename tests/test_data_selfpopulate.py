"""Stage (c): a service self-populates its data slice from the server."""

from __future__ import annotations

import asyncio
import subprocess

import httpx
import pytest

from kenzy import backup, serviceauth, serviceboot
from kenzy.server.server import AudioServer

_NO_KEEPALIVE = httpx.Limits(max_keepalive_connections=0)


# ---------------------------------------------------------------------------
# Slice helpers (pure)
# ---------------------------------------------------------------------------


def test_slice_pack_and_populated_guard(tmp_path):
    home = tmp_path / "home"
    (home / "data" / "speakers").mkdir(parents=True)
    (home / "data" / "speakers" / "alice.npy").write_bytes(b"\x93NUMPY")

    assert backup.slice_populated(home, "speaker")  # a host with data won't pull
    assert not backup.slice_populated(tmp_path / "empty", "speaker")

    blob = backup.create_data_slice(home, "speaker")
    entries = backup.unpack_archive_bytes(blob)
    assert entries == {"data/speakers/alice.npy": b"\x93NUMPY"}


def test_write_slice_roundtrip(tmp_path):
    dst = tmp_path / "dst"
    written = backup.write_slice({"data/speakers/bob.npy": b"xyz"}, dst)
    assert written == ["data/speakers/bob.npy"]
    assert (dst / "data" / "speakers" / "bob.npy").read_bytes() == b"xyz"


def test_llm_slice_covers_skills_and_curation(tmp_path):
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    (home / "skills" / "custom.py").write_text("# skill\n")
    (home / "data" / "home_assistant").mkdir(parents=True)
    (home / "data" / "home_assistant" / "curation.yaml").write_text("devices: {}\n")
    entries = backup.unpack_archive_bytes(backup.create_data_slice(home, "llm"))
    assert set(entries) == {"skills/custom.py", "data/home_assistant/curation.yaml"}


# ---------------------------------------------------------------------------
# End-to-end: empty host pulls its slice from a live TLS server
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def certpair(tmp_path_factory):
    d = tmp_path_factory.mktemp("data_tls")
    cert, key = d / "s.crt", d / "s.key"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
            "-keyout", str(key), "-out", str(cert), "-subj", "/CN=data-e2e",
        ],
        check=True,
        capture_output=True,
    )
    return str(cert), str(key)


async def test_server_serves_signed_data_slice(certpair, tmp_path, monkeypatch):
    # The server's config home holds an enrolled embedding; a service pulls it
    # over the signed channel and the response verifies against the server cert.
    server_home = tmp_path / "server"
    (server_home / "data" / "speakers").mkdir(parents=True)
    (server_home / "data" / "speakers" / "alice.npy").write_bytes(b"\x93NUMPYalice")
    monkeypatch.setenv("KENZY_HOME", str(server_home))
    monkeypatch.chdir(server_home)  # kenzy_data_root() resolves here
    cert, key = certpair
    server = AudioServer(
        {
            "host": "127.0.0.1", "port": 8851,
            "tls": {"cert": cert, "key": key},
            "discovery": {"token": "dtok"},
        }
    )
    task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.3)
    try:
        hdr = serviceauth.sign_service_request("dtok", "GET", "/data/speaker")
        async with httpx.AsyncClient(verify=False, limits=_NO_KEEPALIVE) as c:
            r = await c.get(
                "https://127.0.0.1:8851/data/speaker",
                headers={serviceauth.SIG_HEADER: hdr},
            )
            assert r.status_code == 200
            ts = int(hdr.split("ts=")[1].split(",")[0])
            assert serviceauth.verify_service_response(
                r.headers.get("X-Kenzy-Sig"), "dtok", ts, r.content,
                binding=server._channel_binding,
            )
            entries = backup.unpack_archive_bytes(r.content)
            assert entries == {"data/speakers/alice.npy": b"\x93NUMPYalice"}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_empty_host_self_populates_then_local_wins(certpair, tmp_path, monkeypatch):
    # A server with data; an empty "service host" boots and self-populates. A
    # second run (now populated) must NOT call the server again.
    server_home = tmp_path / "server"
    (server_home / "data" / "speakers").mkdir(parents=True)
    (server_home / "data" / "speakers" / "carol.npy").write_bytes(b"carol-embedding")
    host_home = tmp_path / "svchost"
    host_home.mkdir()

    cert, key = certpair
    # The server captures its data root at construction — set the server home
    # BEFORE creating it, so it serves from server_home.
    monkeypatch.setenv("KENZY_HOME", str(server_home))
    monkeypatch.chdir(server_home)
    server = AudioServer(
        {
            "host": "127.0.0.1", "port": 8852,
            "tls": {"cert": cert, "key": key},
            "discovery": {"token": "dtok2"},
        }
    )
    task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.3)
    try:
        # now act as the SERVICE host: its data root is the empty host_home
        monkeypatch.setenv("KENZY_HOME", str(host_home))
        monkeypatch.setenv("KENZY_SERVICE_TOKEN", "dtok2")
        monkeypatch.setenv("KENZY_SERVER_URL", "wss://127.0.0.1:8852")
        monkeypatch.delenv("KENZY_TLS_VERIFY", raising=False)
        monkeypatch.chdir(host_home)
        serviceboot._server_base = None

        await asyncio.to_thread(serviceboot.populate_data, "speaker")
        assert (host_home / "data" / "speakers" / "carol.npy").read_bytes() == b"carol-embedding"

        # local wins: mutate the local copy, run again, confirm it's untouched
        (host_home / "data" / "speakers" / "carol.npy").write_bytes(b"LOCAL-EDIT")
        await asyncio.to_thread(serviceboot.populate_data, "speaker")
        assert (host_home / "data" / "speakers" / "carol.npy").read_bytes() == b"LOCAL-EDIT"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
