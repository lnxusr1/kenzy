"""Dashboard restore: the server-side apply of an uploaded backup."""

from __future__ import annotations

from pathlib import Path

from kenzy import backup
from kenzy.server.server import AudioServer


def _seed(root: Path) -> None:
    (root / "configs" / "nodes").mkdir(parents=True)
    (root / "data" / "speakers").mkdir(parents=True)
    (root / "skills").mkdir()
    (root / "configs" / "server.yaml").write_text("port: 8765\n")
    (root / "configs" / "nodes" / "office.yaml").write_text("room_id: office\n")
    (root / "data" / "speakers" / "alice.npy").write_bytes(b"\x93NUMPYalice")
    (root / "skills" / "custom.py").write_text("# my skill\n")


def test_restore_from_archive_writes_into_data_root(tmp_path, monkeypatch):
    # a backup made from a source tree
    src = tmp_path / "src"
    _seed(src)
    archive = backup.create_backup(src)

    # a fresh server whose config home is empty
    dst = tmp_path / "server_home"
    dst.mkdir()
    monkeypatch.setenv("KENZY_HOME", str(dst))
    monkeypatch.chdir(dst)
    server = AudioServer({})  # captures _data_root = dst

    restored = server.restore_from_archive(archive)

    assert (dst / "data" / "speakers" / "alice.npy").read_bytes() == b"\x93NUMPYalice"
    assert (dst / "skills" / "custom.py").read_text() == "# my skill\n"  # custom skills included
    assert (dst / "configs" / "nodes" / "office.yaml").exists()
    assert "skills/custom.py" in restored


def test_restore_force_overwrites_existing(tmp_path, monkeypatch):
    src = tmp_path / "src"
    _seed(src)
    (src / "configs" / "nodes" / "office.yaml").write_text("room_id: NEW\n")
    archive = backup.create_backup(src)

    dst = tmp_path / "server_home"
    (dst / "configs" / "nodes").mkdir(parents=True)
    (dst / "configs" / "nodes" / "office.yaml").write_text("room_id: OLD\n")
    monkeypatch.setenv("KENZY_HOME", str(dst))
    monkeypatch.chdir(dst)
    server = AudioServer({})

    server.restore_from_archive(archive)  # dashboard restore is a force overwrite
    assert "room_id: NEW" in (dst / "configs" / "nodes" / "office.yaml").read_text()


def test_restore_bad_archive_raises(tmp_path, monkeypatch):
    dst = tmp_path / "server_home"
    dst.mkdir()
    monkeypatch.setenv("KENZY_HOME", str(dst))
    monkeypatch.chdir(dst)
    server = AudioServer({})
    import pytest

    with pytest.raises(Exception):
        server.restore_from_archive(b"not a tarball")
