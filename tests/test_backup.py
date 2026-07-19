"""Tests for config-home backup & restore (kenzy.backup, the dashboard download
route, and the safety rails: secrets never travel, no traversal, no clobber)."""

from __future__ import annotations

import io
import json
import tarfile

import pytest

from kenzy.backup import (
    MANIFEST_NAME,
    RestoreError,
    create_backup,
    read_manifest,
    restore_backup,
)


def _make_home(root):
    """A representative config home, including things that must NOT be backed up."""
    (root / "configs" / "nodes").mkdir(parents=True)
    (root / "configs" / "services").mkdir(parents=True)
    (root / "data" / "speakers").mkdir(parents=True)
    (root / "data" / "home_assistant").mkdir(parents=True)
    (root / "skills").mkdir()
    (root / "models" / "speaker").mkdir(parents=True)
    (root / "configs" / "server.yaml").write_text("port: 8765\n")
    (root / "configs" / "server.local.yaml").write_text("dashboard:\n  logs: true\n")
    (root / "configs" / "nodes" / "office.yaml").write_text("room_id: office\n")
    (root / "configs" / "services" / "llm.yaml").write_text("model: gpt-4o\n")
    (root / "data" / "speakers" / "alice.npy").write_bytes(b"\x93NUMPY-fake")
    (root / "data" / "home_assistant" / "curation.yaml").write_text("devices: {}\n")
    (root / "skills" / "my_skill.py").write_text("# custom\n")
    (root / "skills" / "__pycache__").mkdir()
    (root / "skills" / "__pycache__" / "my_skill.pyc").write_bytes(b"\x00")
    (root / "constraints.txt").write_text("numpy<2.0\n")
    (root / ".env").write_text("OPENAI_API_KEY=sk-secret\n")
    (root / "models" / "speaker" / "big.bin").write_bytes(b"\x00" * 64)
    # TLS material — a private key must NEVER enter an archive, wherever it sits.
    (root / "configs" / "certs").mkdir()
    (root / "configs" / "certs" / "kenzy.crt").write_text("-----BEGIN CERTIFICATE-----\n")
    (root / "configs" / "certs" / "kenzy.key").write_text("-----BEGIN PRIVATE KEY-----\n")
    (root / "stray.pem").write_text("-----BEGIN PRIVATE KEY-----\n")


def _names(data: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return {m.name for m in tar.getmembers()}


def test_backup_includes_state_and_excludes_secrets_and_models(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _make_home(home)
    names = _names(create_backup(home))

    assert "configs/server.yaml" in names
    assert "configs/server.local.yaml" in names
    assert "configs/nodes/office.yaml" in names
    assert "data/speakers/alice.npy" in names  # the un-regenerable part
    assert "data/home_assistant/curation.yaml" in names
    assert "skills/my_skill.py" in names
    assert "constraints.txt" in names
    assert MANIFEST_NAME in names

    assert not any(".env" in n for n in names)  # secrets never travel
    assert not any(n.startswith("models/") for n in names)  # re-downloadable bulk
    assert not any("__pycache__" in n for n in names)
    assert not any("certs/" in n for n in names)  # TLS key/cert never travel
    assert not any(n.endswith((".key", ".pem")) for n in names)  # private key material


def test_restore_roundtrip(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _make_home(home)
    archive = tmp_path / "backup.tar.gz"
    archive.write_bytes(create_backup(home))

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    restored = restore_backup(archive, fresh)
    assert "data/speakers/alice.npy" in restored
    assert (fresh / "configs" / "server.yaml").read_text() == "port: 8765\n"
    assert (fresh / "data" / "speakers" / "alice.npy").read_bytes() == b"\x93NUMPY-fake"
    assert not (fresh / ".env").exists()

    m = read_manifest(archive)
    assert m and "kenzy_version" in m and "created" in m


def test_restore_refuses_collisions_without_force(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _make_home(home)
    archive = tmp_path / "backup.tar.gz"
    archive.write_bytes(create_backup(home))

    target = tmp_path / "existing"
    (target / "configs").mkdir(parents=True)
    (target / "configs" / "server.yaml").write_text("port: 9999\n")

    with pytest.raises(RestoreError, match="--force"):
        restore_backup(archive, target)
    # Refusal is atomic: the collision is untouched AND nothing else was written.
    assert (target / "configs" / "server.yaml").read_text() == "port: 9999\n"
    assert not (target / "skills").exists()

    restore_backup(archive, target, force=True)
    assert (target / "configs" / "server.yaml").read_text() == "port: 8765\n"


def _evil_archive(tmp_path, name: str, *, symlink: bool = False):
    path = tmp_path / "evil.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        if symlink:
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
        else:
            data = b"evil"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


@pytest.mark.parametrize(
    "member",
    ["../outside.yaml", "/etc/cron.d/evil", "configs/../../outside", "evil.sh"],
)
def test_restore_rejects_unsafe_paths(tmp_path, member):
    archive = _evil_archive(tmp_path, member)
    with pytest.raises(RestoreError):
        restore_backup(archive, tmp_path / "out")


def test_restore_rejects_symlinks_and_skips_env(tmp_path):
    with pytest.raises(RestoreError, match="unsupported"):
        restore_backup(_evil_archive(tmp_path, "configs/link.yaml", symlink=True), tmp_path / "o")

    # A hand-built archive can't smuggle a .env in: it's skipped, not restored.
    path = tmp_path / "sneaky.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for name, data in (("configs/server.yaml", b"port: 1\n"), ("configs/.env", b"KEY=x")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    out = tmp_path / "out2"
    restored = restore_backup(path, out)
    assert restored == ["configs/server.yaml"]
    assert not (out / "configs" / ".env").exists()


def test_backup_opt_in_secrets_and_models(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _make_home(home)

    names = _names(create_backup(home, include_secrets=True))
    assert ".env" in names and not any(n.startswith("models/") for n in names)

    names = _names(create_backup(home, include_models=True))
    assert "models/speaker/big.bin" in names and ".env" not in names

    # And a "secrets" archive restores its .env (still collision-guarded).
    archive = tmp_path / "b.tar.gz"
    archive.write_bytes(create_backup(home, include_secrets=True, include_models=True))
    out = tmp_path / "out"
    restore_backup(archive, out)
    assert (out / ".env").read_text() == "OPENAI_API_KEY=sk-secret\n"
    assert (out / "models" / "speaker" / "big.bin").exists()

    m = read_manifest(archive)
    assert m and m["includes_secrets"] is True and m["includes_models"] is True


def test_service_slices_cannot_poison_the_archive(tmp_path):
    from kenzy.backup import archive_entries, unpack_archive_bytes

    # A (hostile/buggy) service slice tries to inject configs and a .env.
    slice_bytes = archive_entries(
        {
            "data/speakers/bob.npy": b"ok",
            "configs/server.yaml": b"evil",
            ".env": b"KEY=stolen",
            "../escape": b"x",
        },
        None,
    )
    entries = unpack_archive_bytes(slice_bytes)
    assert entries == {"data/speakers/bob.npy": b"ok"}  # only its own state survives

    # And create_backup's merge applies the same scope + local-wins.
    home = tmp_path / "home"
    home.mkdir()
    _make_home(home)
    names_to_data = {
        "data/speakers/bob.npy": b"ok",
        "data/speakers/alice.npy": b"remote-should-lose",
        "configs/server.yaml": b"evil",
    }
    archive = create_backup(home, extra_entries=names_to_data)
    import io as _io
    import tarfile as _tarfile

    with _tarfile.open(fileobj=_io.BytesIO(archive), mode="r:gz") as tar:
        alice = tar.extractfile("data/speakers/alice.npy")
        server_yaml = tar.extractfile("configs/server.yaml")
        assert alice is not None and alice.read() == b"\x93NUMPY-fake"  # local wins
        assert server_yaml is not None and server_yaml.read() == b"port: 8765\n"
        assert tar.extractfile("data/speakers/bob.npy") is not None  # new state merged


def test_service_backup_endpoint_slice(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from kenzy.backup import unpack_archive_bytes
    from kenzy.fastapi_auth import install_backup_endpoint

    emb = tmp_path / "emb"
    emb.mkdir()
    (emb / "alice.npy").write_bytes(b"npy")
    app = FastAPI()
    install_backup_endpoint(app, lambda: [(emb, "data/speakers")])
    r = TestClient(app).get("/backup")
    assert r.status_code == 200
    assert unpack_archive_bytes(r.content) == {"data/speakers/alice.npy": b"npy"}


async def test_server_merges_service_slices(tmp_path, monkeypatch):
    from kenzy.server.server import TranscribingServer

    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    _make_home(tmp_path)
    s = TranscribingServer(
        {"speaker": {"url": "http://sp:1/identify"}, "llm": {"url": "http://llm:2/process"}}
    )

    async def fake_slice(base_url: str) -> dict[str, bytes]:
        if base_url.startswith("http://sp"):
            return {"data/speakers/remote-bob.npy": b"npy"}
        raise OSError("llm down")

    monkeypatch.setattr(s, "_fetch_backup_slice", fake_slice)
    data = await s.create_backup_archive()
    names = _names(data)
    assert "data/speakers/remote-bob.npy" in names  # merged from the speaker host
    assert "data/speakers/alice.npy" in names  # local state intact

    import io as _io
    import tarfile as _tarfile

    with _tarfile.open(fileobj=_io.BytesIO(data), mode="r:gz") as tar:
        f = tar.extractfile(MANIFEST_NAME)
        assert f is not None
        manifest = json.loads(f.read())
    assert manifest["service_slices"] == {"speaker": "1 file(s)", "llm": "unreachable"}


def test_set_env_secret(tmp_path, monkeypatch):
    from kenzy.server.server import AudioServer

    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    monkeypatch.delenv("MY_TEST_KEY", raising=False)
    s = AudioServer({})

    s.set_env_secret("MY_TEST_KEY", "abc123")  # creates the file
    env = tmp_path / ".env"
    assert env.read_text() == 'MY_TEST_KEY="abc123"\n'
    import os

    assert os.environ["MY_TEST_KEY"] == "abc123"
    monkeypatch.delenv("MY_TEST_KEY")

    # Replaces in place (incl. an export-style line), preserving other lines.
    env.write_text('# keys\nexport MY_TEST_KEY="old"\nOTHER="keep"\n')
    s.set_env_secret("MY_TEST_KEY", "new456")
    assert env.read_text() == '# keys\nMY_TEST_KEY="new456"\nOTHER="keep"\n'
    monkeypatch.delenv("MY_TEST_KEY")

    import pytest as _pytest

    for bad_name in ("lower", "1BAD", "SPACE Y", ""):
        with _pytest.raises(ValueError):
            s.set_env_secret(bad_name, "x")
    for bad_value in ("", "two\nlines", 'has"quote'):
        with _pytest.raises(ValueError):
            s.set_env_secret("GOOD_NAME", bad_value)


async def test_dashboard_backup_route(tmp_path, monkeypatch):
    from websockets.datastructures import Headers
    from websockets.http11 import Request

    from kenzy.server.dashboard import Dashboard, DashboardConfig
    from kenzy.server.server import AudioServer

    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    _make_home(tmp_path)
    d = Dashboard(AudioServer({}), {}, DashboardConfig(enabled=True, auth_token="tok"))

    def req(bearer=None):
        h = Headers()
        if bearer:
            h["Authorization"] = f"Bearer {bearer}"
        return Request("/api/backup", h)

    resp = await d.process_request(None, req())  # unauthenticated → 401
    assert resp is not None and resp.status_code == 401

    resp = await d.process_request(None, req("tok"))
    assert resp is not None and resp.status_code == 200
    assert resp.headers["Content-Type"] == "application/gzip"
    assert "kenzy-backup-" in resp.headers["Content-Disposition"]
    names = _names(resp.body)
    assert "configs/server.yaml" in names and not any(".env" in n for n in names)
    with tarfile.open(fileobj=io.BytesIO(resp.body), mode="r:gz") as tar:
        f = tar.extractfile(MANIFEST_NAME)
        assert f is not None and json.loads(f.read())["kenzy_version"]


def test_restore_regenerates_missing_tls_cert(tmp_path):
    """A restored server.yaml with a tls: block whose files are absent (they
    never travel in a backup) gets a fresh self-signed pair — so TLS survives a
    restore instead of silently degrading to plaintext."""
    import shutil

    import pytest

    from kenzy.init import _ensure_restored_certs

    if not shutil.which("openssl"):
        pytest.skip("openssl not available")
    home = tmp_path / "home"
    (home / "configs" / "certs").mkdir(parents=True)
    cert = home / "configs" / "certs" / "kenzy.crt"
    key = home / "configs" / "certs" / "kenzy.key"
    (home / "configs" / "server.yaml").write_text(
        f"port: 8765\ntls:\n  cert: {cert}\n  key: {key}\n"
    )
    assert not cert.exists()
    _ensure_restored_certs(home)
    assert cert.is_file() and key.is_file()
    assert oct(key.stat().st_mode)[-3:] == "600"  # key is private


def test_restore_without_tls_makes_no_cert(tmp_path):
    from kenzy.init import _ensure_restored_certs

    home = tmp_path / "home"
    (home / "configs").mkdir(parents=True)
    (home / "configs" / "server.yaml").write_text("port: 8765\n")
    _ensure_restored_certs(home)  # no tls block -> no-op, no crash
    assert not (home / "configs" / "certs").exists()


def test_restore_relocates_absolute_cert_to_new_home(tmp_path):
    """A backup restored into a DIFFERENT folder: server.yaml references the old
    machine's absolute cert path. The regen relocates the pair under the new home
    and rewrites the tls block — so a cross-machine restore keeps TLS."""
    import shutil

    import pytest

    from kenzy.backup import regenerate_missing_certs

    if not shutil.which("openssl"):
        pytest.skip("openssl not available")
    new_home = tmp_path / "newmachine"
    (new_home / "configs").mkdir(parents=True)
    old = "/some/old/machine/configs/certs"  # absent on this box
    (new_home / "configs" / "server.yaml").write_text(
        f'port: 8765\ntls:\n  cert: {old}/kenzy.crt\n  key: {old}/kenzy.key\n'
    )
    msg = regenerate_missing_certs(new_home)
    assert msg and "Relocated" in msg
    # cert now lives under the new home, not the stale old path
    assert (new_home / "certs" / "kenzy.crt").is_file()
    assert oct((new_home / "certs" / "kenzy.key").stat().st_mode)[-3:] == "600"
    # server.yaml points at the relocated pair, old path gone
    text = (new_home / "configs" / "server.yaml").read_text()
    assert str(new_home / "certs" / "kenzy.crt") in text
    assert old not in text


def test_lockbox_key_rides_backup_by_default(tmp_path):
    # Founder call 2026-07-18: a backup's job is to restore EVERYTHING — the
    # lockbox key rides by default; include_lockbox_key=False builds the
    # shareable (ciphertext-only) archive.
    import io
    import tarfile

    from kenzy import backup

    (tmp_path / "data" / "memory").mkdir(parents=True)
    (tmp_path / "data" / "memory" / "lockbox.enc").write_bytes(b"cipher")
    (tmp_path / "data" / "memory" / "lockbox.key").write_bytes(b"KEY")
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "server.yaml").write_text("x: 1\n")

    def names(blob):
        with tarfile.open(fileobj=io.BytesIO(blob)) as t:
            return set(t.getnames())

    full = names(backup.create_backup(tmp_path))
    assert "data/memory/lockbox.key" in full and "data/memory/lockbox.enc" in full

    shareable = names(backup.create_backup(tmp_path, include_lockbox_key=False))
    assert "data/memory/lockbox.key" not in shareable
    assert "data/memory/lockbox.enc" in shareable  # ciphertext still rides

    # TLS-style key material stays out of BOTH.
    (tmp_path / "configs" / "certs").mkdir()
    (tmp_path / "configs" / "certs" / "server.key").write_bytes(b"TLS")
    assert not any(n.endswith("server.key") for n in names(backup.create_backup(tmp_path)))

    # And a restore applies the key (round-trip: the whole point).
    blob = backup.create_backup(tmp_path)
    arc = tmp_path / "b.tar.gz"
    arc.write_bytes(blob)
    target = tmp_path / "restored"
    target.mkdir()
    backup.restore_backup(arc, target, force=True)
    assert (target / "data" / "memory" / "lockbox.key").read_bytes() == b"KEY"
