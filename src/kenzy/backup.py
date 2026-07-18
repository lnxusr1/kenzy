"""
Config-home backup & restore.

The config home accumulates irreplaceable state: per-node and per-service
overrides, `server.local.yaml`, the Home Assistant curation file, **speaker
voice embeddings** (which cannot be regenerated without re-enrolling every
voice), `constraints.txt`, and the user skills overlay. This module turns
"my SD card died" into a five-minute recovery instead of a re-enroll-everyone
event: the dashboard serves a downloadable archive (`GET /api/backup`), and
`kenzy-init --restore` unpacks one into a config home.

Scope is a **whitelist** of the state-bearing top-level entries — never "the
whole directory" — because in a dev checkout the config home is the repo root.
Secrets are excluded by design: `.env` never enters an archive (and is refused
out of one), consistent with secrets-never-served; copy it by hand.
"""

from __future__ import annotations

import io
import json
import logging
import tarfile
import time
from pathlib import Path

from kenzy import kenzy_version

log = logging.getLogger(__name__)

#: Top-level entries (relative to the config home) that carry restorable state.
BACKUP_INCLUDE = ("configs", "skills", "data", "constraints.txt")

#: What a *restore* accepts: the standard set plus the opt-in extras — ``models/``
#: ("include everything") and the top-level ``.env`` ("include secrets").
RESTORE_INCLUDE = (*BACKUP_INCLUDE, "models")

#: What a backend service's /backup slice may contribute (its own state only —
#: a misbehaving service must never be able to inject configs or a .env).
SLICE_INCLUDE = ("data", "skills")

#: Manifest written into every archive (version/date provenance for support).
MANIFEST_NAME = "kenzy-backup.json"


class RestoreError(Exception):
    """A restore that must not proceed (bad archive, or collisions without force)."""


def _excluded(rel: Path) -> bool:
    """Per-file exclusions inside the whitelisted trees."""
    if any(part in ("__pycache__", ".venv", ".git", "certs") for part in rel.parts):
        return True  # certs/ holds a TLS private key — host security material, like .env
    if rel.name == ".env" or rel.name.endswith(".env"):
        return True  # secrets never enter an archive
    if rel.name.endswith((".key", ".pem")):
        return True  # a private key never enters an archive, wherever it sits
    return rel.name.endswith(".pyc")


def safe_entry_name(name: str, *, roots: tuple[str, ...], allow_env: bool = False) -> bool:
    """Whether an archive entry name is acceptable in the given scope: relative,
    traversal-free, rooted in ``roots``, and not a secret/cache exclusion.
    ``allow_env`` admits exactly the top-level ``.env`` (secrets-included
    backups) — never a nested one."""
    if name == ".env":
        return allow_env
    rel = Path(name)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        return False
    if rel.parts[0] not in roots:
        return False
    return not _excluded(rel)


def collect_local(root: Path) -> dict[str, bytes]:
    """The config home's whitelisted state as ``{archive name: bytes}``."""
    entries: dict[str, bytes] = {}
    for top in BACKUP_INCLUDE:
        path = root / top
        if path.is_file():
            entries[top] = path.read_bytes()
        elif path.is_dir():
            for member in sorted(p for p in path.rglob("*") if p.is_file()):
                rel = member.relative_to(root)
                if not _excluded(rel):
                    entries[str(rel)] = member.read_bytes()
    return entries


#: Per-service data slice = the config-home-relative paths a service owns. The
#: server serves these from ITS config home (``GET /data/<svc>``); a freshly
#: installed service self-populates them at boot when its own copy is empty.
#: Same scope as the backup slices (``data/`` + ``skills/``) so it reuses the
#: same safe archiving/extraction; the path doubles as the archive prefix.
DATA_SLICES: dict[str, tuple[str, ...]] = {
    "speaker": ("data/speakers",),
    "llm": ("skills", "data/home_assistant", "data/memory"),
}


def create_data_slice(root: Path, service: str) -> bytes:
    """A service's data slice as a tar.gz, taken from config home ``root``."""
    pairs = [(root / p, p) for p in DATA_SLICES.get(service, ())]
    return archive_entries(collect_paths(pairs), None)


def slice_populated(root: Path, service: str) -> bool:
    """True when the service's slice already holds a file locally — the guard
    that makes self-population fill only an EMPTY host and never clobber a live
    one (local data always wins)."""
    for prefix in DATA_SLICES.get(service, ()):
        d = root / prefix
        if d.is_dir() and any(p.is_file() for p in d.rglob("*")):
            return True
    return False


def write_slice(entries: dict[str, bytes], root: Path) -> list[str]:
    """Write unpacked slice entries (already scope-validated) into ``root``."""
    written: list[str] = []
    for name in sorted(entries):
        dest = root / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(entries[name])
        written.append(name)
    return written


def collect_paths(pairs: list[tuple[Path, str]]) -> dict[str, bytes]:
    """State slice for a backend service: each (path, arcprefix) pair maps a
    local file or directory tree onto canonical archive names."""
    entries: dict[str, bytes] = {}
    for path, prefix in pairs:
        if path.is_file():
            entries[prefix] = path.read_bytes()
        elif path.is_dir():
            for member in sorted(p for p in path.rglob("*") if p.is_file()):
                name = f"{prefix}/{member.relative_to(path)}"
                if safe_entry_name(name, roots=SLICE_INCLUDE):
                    entries[name] = member.read_bytes()
    return entries


def archive_entries(entries: dict[str, bytes], manifest: dict[str, object] | None) -> bytes:
    """Pack ``{name: bytes}`` (plus an optional manifest) into a gzipped tar."""
    buf = io.BytesIO()
    now = int(time.time())
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if manifest is not None:
            data = json.dumps(manifest, indent=2).encode()
            info = tarfile.TarInfo(MANIFEST_NAME)
            info.size = len(data)
            info.mtime = now
            tar.addfile(info, io.BytesIO(data))
        for name in sorted(entries):
            info = tarfile.TarInfo(name)
            info.size = len(entries[name])
            info.mtime = now
            tar.addfile(info, io.BytesIO(entries[name]))
    return buf.getvalue()


def unpack_archive_bytes(data: bytes) -> dict[str, bytes]:
    """Entries of an in-memory archive (a fetched service slice), name-validated
    against the slice scope (``data/``+``skills/`` — never configs or ``.env``).

    Unsafe or off-scope names are dropped, not fatal — one misbehaving service
    must not break (or poison) the whole backup.
    """
    entries: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            if (
                not m.isreg()
                or m.name == MANIFEST_NAME
                or not safe_entry_name(m.name, roots=SLICE_INCLUDE)
            ):
                continue
            f = tar.extractfile(m)
            if f is not None:
                entries[m.name] = f.read()
    return entries


def create_backup(
    root: Path,
    extra_entries: dict[str, bytes] | None = None,
    notes: dict[str, object] | None = None,
    *,
    include_secrets: bool = False,
    include_models: bool = False,
) -> bytes:
    """Build a gzipped tar of the config home's state; returns the bytes.

    ``extra_entries`` are merged-in slices fetched from the backend services
    (speaker embeddings, skills/curation on the LLM host) so a multi-host
    deployment still yields ONE complete archive; **local wins** on name
    collisions, which is also what dedupes the co-located case (same files,
    same names). ``notes`` lands in the manifest (per-slice provenance).

    ``include_secrets`` adds the server host's top-level ``.env`` (opt-in — the
    archive then carries live API keys; treat it accordingly). ``include_models``
    adds the local ``models/`` tree (opt-in — normally excluded as re-downloadable
    bulk, but the only way to capture a hand-placed custom model).
    """
    entries = collect_local(root)
    for name, data in (extra_entries or {}).items():
        if safe_entry_name(name, roots=SLICE_INCLUDE):
            entries.setdefault(name, data)
    if include_secrets and (root / ".env").is_file():
        entries[".env"] = (root / ".env").read_bytes()
    if include_models and (root / "models").is_dir():
        for member in sorted(p for p in (root / "models").rglob("*") if p.is_file()):
            rel = member.relative_to(root)
            if not _excluded(rel):
                entries[str(rel)] = member.read_bytes()
    manifest: dict[str, object] = {
        "kenzy_version": kenzy_version(),
        "created": time.time(),
        "root": root.name,
        "includes_secrets": include_secrets,
        "includes_models": include_models,
    }
    if notes:
        manifest.update(notes)
    return archive_entries(entries, manifest)


def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Regular-file members with validated relative paths (no traversal, no links).

    Accepts the opt-in extras a "full"/"secrets" backup legitimately carries
    (``models/``, the top-level ``.env``); everything else off-whitelist is fatal.
    """
    members: list[tarfile.TarInfo] = []
    for m in tar.getmembers():
        if m.name == MANIFEST_NAME or m.isdir():
            continue
        if not m.isreg():  # symlinks/devices/etc. have no business in a config backup
            raise RestoreError(f"unsupported member type in archive: {m.name!r}")
        if safe_entry_name(m.name, roots=RESTORE_INCLUDE, allow_env=True):
            members.append(m)
            continue
        rel = Path(m.name)
        if not rel.is_absolute() and ".." not in rel.parts and rel.parts and _excluded(rel):
            continue  # nested cache/secret exclusions: skipped, not fatal
        raise RestoreError(f"unsafe or unexpected path in archive: {m.name!r}")
    return members


def read_manifest(archive: Path) -> dict[str, object] | None:
    """The archive's manifest, or None when absent/unreadable."""
    try:
        with tarfile.open(archive, "r:gz") as tar:
            f = tar.extractfile(MANIFEST_NAME)
            data = json.loads(f.read()) if f else None
            return data if isinstance(data, dict) else None
    except Exception:
        return None


def regenerate_missing_certs(home: Path) -> str | None:
    """Mint a self-signed TLS pair when the restored config expects one that isn't
    there. A backup never carries the private key, and Kenzy's no-pinning posture
    makes a fresh cert a non-event — without this a restored server would find its
    cert missing and silently fall back to plaintext.

    **Relocation-aware**: ``server.yaml`` stores *absolute* cert paths, so a
    backup restored into a different folder (a new machine) references the old
    install's paths. When the referenced cert is absent, the new pair is minted
    **under this config home** (``<home>/certs/``) and the ``tls:`` block is
    rewritten to match — so restore is location-independent. Returns a status
    message (for the caller to print/log), or ``None`` when nothing needed doing.
    """
    import shutil
    import socket
    import subprocess

    import yaml  # type: ignore[import-untyped]

    files = ("server.yaml", "server.local.yaml")
    tls: dict[str, str] = {}
    for name in files:
        path = home / "configs" / name
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except Exception:
            continue
        block = data.get("tls") if isinstance(data, dict) else None
        if isinstance(block, dict):
            tls.update({k: str(v) for k, v in block.items() if k in ("cert", "key") and v})

    cert, key = tls.get("cert"), tls.get("key")
    if not (cert and key):
        return None  # no TLS configured
    if Path(cert).is_file() and Path(key).is_file():
        return None  # certs still present (same-machine reinstall) — keep them
    if not shutil.which("openssl"):
        return "TLS is configured but openssl isn't available — the server starts in PLAINTEXT."

    # If the stored path already lives inside this config home, regenerate in
    # place (a same-layout reinstall). Only when it points OUTSIDE — a backup
    # restored into a different folder — relocate the pair under this home
    # (``<home>/certs/``, the installer's layout) so TLS survives the move.
    def _inside_home(p: str) -> bool:
        try:
            Path(p).resolve().relative_to(home.resolve())
            return True
        except ValueError:
            return False

    if _inside_home(cert) and _inside_home(key):
        new_cert, new_key = Path(cert), Path(key)
    else:
        new_cert = home / "certs" / Path(cert).name
        new_key = home / "certs" / Path(key).name
    new_cert.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "3650",
                "-keyout",
                str(new_key),
                "-out",
                str(new_cert),
                "-subj",
                f"/CN={socket.gethostname()}",
            ],
            check=True,
            capture_output=True,
        )
        new_key.chmod(0o600)
    except (OSError, subprocess.CalledProcessError) as exc:
        return f"Could not regenerate the TLS certificate ({exc}); the server starts plaintext."

    relocated = str(new_cert) != cert or str(new_key) != key
    if relocated:
        # Point server.yaml / server.local.yaml at the new pair (literal path
        # swap, so surrounding comments and formatting are preserved).
        for name in files:
            path = home / "configs" / name
            if not path.is_file():
                continue
            text = path.read_text()
            if cert in text or key in text:
                path.write_text(text.replace(cert, str(new_cert)).replace(key, str(new_key)))
        return (
            f"Relocated the TLS certificate to {new_cert.parent} and updated "
            "server.yaml (cross-folder restore keeps TLS on)."
        )
    return f"Regenerated a self-signed TLS certificate at {new_cert} (restore keeps TLS on)."


def restore_backup(archive: Path, root: Path, *, force: bool = False) -> list[str]:
    """Unpack a backup into ``root``; returns the restored relative paths.

    Refuses to overwrite existing files unless ``force`` — and checks *all*
    collisions before writing anything, so a refused restore changes nothing.
    """
    with tarfile.open(archive, "r:gz") as tar:
        members = _safe_members(tar)
        if not members:
            raise RestoreError("archive contains no restorable files")

        collisions = [m.name for m in members if (root / m.name).exists()]
        if collisions and not force:
            listing = ", ".join(collisions[:5]) + ("…" if len(collisions) > 5 else "")
            raise RestoreError(
                f"{len(collisions)} file(s) already exist ({listing}) — "
                "re-run with --force to overwrite"
            )

        restored: list[str] = []
        for m in members:
            target = root / m.name
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(m)
            assert src is not None  # _safe_members keeps regular files only
            target.write_bytes(src.read())
            restored.append(m.name)
    log.info("Restored %d file(s) into %s", len(restored), root)
    return restored
