"""
kenzy-deploy: remote deployment and management for kenzy services.

Syncs source, manages Python virtualenvs, writes systemd unit files, and
controls services across a fleet of Debian hosts over SSH.

Prerequisites on each remote host
----------------------------------
  - SSH key authentication (no password prompts)
  - Passwordless sudo  (NOPASSWD in /etc/sudoers)

Run kenzy-deploy init first to satisfy OS-level dependencies, then
kenzy-deploy install for the first full deployment.  Subsequent updates
are a single: kenzy-deploy upgrade

Usage
-----
  kenzy-deploy [--config PATH] [--host NAME] <command> [args]

  Commands
    init                 apt-get OS deps, create install directory
    install              sync + venv + pip + systemd (first time)
    upgrade              sync + pip update + restart services
    status               systemctl status for all services on host(s)
    start   <service>    start a service
    stop    <service>    stop a service
    restart <service>    restart a service
    logs    <service>    tail journald logs  (requires --host)
    uninstall            stop + disable services, remove units + venv
                         (add --purge to also delete the install dir)

Central config (dashboard-managed)
----------------------------------
Backend services (stt/tts/llm/speaker) are installed in *pull mode*: their units
run arg-less so they fetch their effective config from the server (serviceboot),
which keeps them editable from the dashboard. Nodes already pull (node.yaml is
bootstrap-only); set a per-host ``node_id:`` slug in deploy.yaml to give a node a
stable, predictable central record (``configs/nodes/<node_id>.yaml``) — omit it
and the node self-generates a uuid (still dashboard-managed, just opaque).

The server's central store (``configs/nodes/``, ``configs/services/``) is seeded
from the operator tree with ``--ignore-existing`` (seed-don't-clobber), so a
re-deploy never overwrites live dashboard edits. ``--reseed`` forces the operator
values back. Pull-mode services need ``KENZY_SERVICE_TOKEN`` (+ mDNS or
``KENZY_SERVER_URL``) in each host's ``.env`` to reach the server.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

# ---------------------------------------------------------------------------
# ANSI colours
# ---------------------------------------------------------------------------

_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"{code}{text}\033[0m" if _TTY else text


def _bold(t: str) -> str:
    return _c("\033[1m", t)


def _green(t: str) -> str:
    return _c("\033[32m", t)


def _red(t: str) -> str:
    return _c("\033[31m", t)


def _yellow(t: str) -> str:
    return _c("\033[33m", t)


def _cyan(t: str) -> str:
    return _c("\033[36m", t)


def _dim(t: str) -> str:
    return _c("\033[2m", t)


def _header(host: str, msg: str) -> None:
    print(f"\n{_bold(_cyan(f'[{host}]'))} {msg}")


def _ok(msg: str) -> None:
    print(f"  {_green('✓')} {msg}")


def _err(msg: str) -> None:
    print(f"  {_red('✗')} {msg}", file=sys.stderr)


def _info(msg: str) -> None:
    print(f"  {_dim('›')} {msg}")


def _warn(msg: str) -> None:
    print(f"  {_yellow('!')} {msg}")


# ---------------------------------------------------------------------------
# Service metadata
# ---------------------------------------------------------------------------

SERVICE_INFO: dict[str, dict[str, str]] = {
    "node": {
        "script": "kenzy-node",
        "config": "configs/node.yaml",
        "desc": "Kenzy Node (wake word + audio capture)",
    },
    "server": {
        "script": "kenzy-server",
        "config": "configs/server.yaml",
        "desc": "Kenzy Server (WebSocket hub + pipeline)",
    },
    "stt": {
        "script": "kenzy-stt",
        "config": "configs/stt.yaml",
        "desc": "Kenzy STT (speech-to-text)",
    },
    "tts": {
        "script": "kenzy-tts",
        "config": "configs/tts.yaml",
        "desc": "Kenzy TTS (text-to-speech)",
    },
    "llm": {
        "script": "kenzy-llm",
        "config": "configs/llm.yaml",
        "desc": "Kenzy LLM (language model)",
    },
    "speaker": {
        "script": "kenzy-speaker",
        "config": "configs/speaker.yaml",
        "desc": "Kenzy Speaker (voice identification)",
    },
}

# OS packages required per service (beyond the base set).
APT_BASE: list[str] = ["python3", "python3-pip", "python3-venv", "git", "rsync"]

APT_EXTRA: dict[str, list[str]] = {
    "node": ["libportaudio2", "portaudio19-dev", "python3-dev"],
    "server": [],
    "stt": ["ffmpeg", "libgomp1"],
    "tts": ["espeak-ng"],
    "llm": [],
    "speaker": ["libportaudio2", "portaudio19-dev", "python3-dev", "libgomp1"],
}

# Files / dirs excluded from the base rsync to the remote.
# Extra paths (skills/, data/, models/, etc.) are synced separately per host
# based on service_sync in deploy.yaml.
RSYNC_EXCLUDES: list[str] = [
    # Match at any depth — intentionally unanchored.
    "__pycache__/",
    "*.pyc",
    ".mypy_cache/",
    ".ruff_cache/",
    "*.egg-info/",
    # Root-anchored: only exclude the top-level directory, not same-named
    # subdirectories inside the package (e.g. src/kenzy/llm/skills/).
    "/.venv/",
    "/skills/",
    "/data/",
    "/models/",
    "/.env",  # secrets stay on each host
    # Central, dashboard-owned override store — seeded separately (seed-don't-
    # clobber) so a re-deploy never overwrites live dashboard edits.
    "/configs/nodes/",
    "/configs/services/",
]

# Backend services that pull their effective config from the server at boot
# (serviceboot). Their systemd units run arg-less so they fetch GET /config/<svc>
# rather than reading a local file — which is what makes them dashboard-managed.
# node + server keep an explicit local config (node.yaml is bootstrap-only; the
# server is the config authority).
_PULL_SERVICES: frozenset[str] = frozenset({"stt", "tts", "llm", "speaker"})

# ---------------------------------------------------------------------------
# Host config
# ---------------------------------------------------------------------------


@dataclass
class HostConfig:
    name: str
    address: str
    ssh_user: str
    install_path: str
    venv_path: str
    python_bin: str
    local: bool = False  # run commands directly instead of over SSH
    services: list[str] = field(default_factory=list)
    sync: list[str] = field(default_factory=list)  # extra paths to sync
    torch_index_url: str | None = None  # PyTorch wheel index; None = auto-detect
    pip_packages: list[str] = field(default_factory=list)  # extra pip installs after main
    install_mode: str = "source"  # "source" (rsync + pip -e) or "pypi" (pip install kenzy)
    version: str | None = None  # pin a PyPI version (pypi mode); None = latest >=3
    constraints: str | None = None  # pip constraints file (rel. to config-root or abs)
    node_id: str | None = None  # operator-chosen stable node_id (slug); None = node self-generates
    server_url: str | None = None  # KENZY_SERVER_URL for pull-mode services (auto-derived)
    listen_all: bool = False  # bind backend services to 0.0.0.0 instead of 127.0.0.1
    extras: list[str] = field(default_factory=list)  # extra pip extras (e.g. kokoro, mqtt)


# ---------------------------------------------------------------------------
# SSH / rsync helpers
# ---------------------------------------------------------------------------

_SSH_OPTS: list[str] = [
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "ConnectTimeout=10",
]


def _run(
    host: HostConfig,
    cmd: str,
    sudo: bool = False,
    check: bool = True,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a shell command on the host — locally or over SSH based on host.local."""
    if sudo:
        cmd = f"sudo {cmd}"

    if host.local:
        result = subprocess.run(
            cmd,
            shell=True,
            input=stdin,
            capture_output=True,
            text=True,
        )
    else:
        result = subprocess.run(
            ["ssh", *_SSH_OPTS, f"{host.ssh_user}@{host.address}", cmd],
            input=stdin,
            capture_output=True,
            text=True,
        )

    if check and result.returncode != 0:
        _err(f"command failed (exit {result.returncode}): {cmd[:80]}")
        for line in (result.stderr or "").strip().splitlines():
            print(f"    {_dim(line)}", file=sys.stderr)
    return result


# Keep _ssh as an alias so callers read naturally.
_ssh = _run


def _ssh_interactive(host: HostConfig, cmd: str) -> None:
    """Run an interactive command — locally or over SSH."""
    if host.local:
        subprocess.run(cmd, shell=True)
    else:
        subprocess.run(["ssh", "-t", *_SSH_OPTS, f"{host.ssh_user}@{host.address}", cmd])


def _rsync(host: HostConfig, local_path: Path) -> bool:
    excludes: list[str] = []
    for exc in RSYNC_EXCLUDES:
        excludes += ["--exclude", exc]

    if host.local:
        # Local: copy within the filesystem (src → install_path).
        # Skip if they're the same directory.
        if local_path.resolve() == Path(host.install_path).resolve():
            _info("install_path matches project root — skipping copy")
            return True
        result = subprocess.run(
            [
                "rsync",
                "-az",
                "--delete",
                "--info=progress2",
                *excludes,
                f"{local_path}/",
                f"{host.install_path}/",
            ],
            text=True,
        )
    else:
        result = subprocess.run(
            [
                "rsync",
                "-az",
                "--delete",
                "--info=progress2",
                *excludes,
                f"{local_path}/",
                f"{host.ssh_user}@{host.address}:{host.install_path}/",
            ],
            text=True,
        )

    if result.returncode != 0:
        _err("rsync failed")
        return False
    return True


def _rsync_host_configs(host: HostConfig, local_path: Path) -> None:
    """Sync per-host config overrides into the remote configs/ directory.

    If configs/hosts/<host-name>/ exists locally, its contents are merged into
    {install_path}/configs/ on the remote.  No --delete so base configs that
    have no overlay counterpart are preserved.
    """
    overlay = local_path / "configs" / "hosts" / host.name
    if not overlay.is_dir():
        return

    dst = f"{host.install_path}/configs"

    if host.local:
        if local_path.resolve() == Path(host.install_path).resolve():
            return  # overlay IS the live configs dir — nothing to copy
        cmd = ["rsync", "-az", "--info=progress2", f"{overlay}/", f"{dst}/"]
    else:
        cmd = [
            "rsync",
            "-az",
            "--info=progress2",
            f"{overlay}/",
            f"{host.ssh_user}@{host.address}:{dst}/",
        ]

    result = subprocess.run(cmd, text=True)
    if result.returncode != 0:
        _err(f"host config overlay failed for {host.name}")
    else:
        _ok(f"host config overlay applied ({overlay.relative_to(local_path)})")


# Remote one-liner that sets node_id in node.yaml: replaces the first node_id line
# (commented or not), else appends. Mirrors kenzy.init._set_node_id; run with the
# host's system python (the venv may not exist yet on first install).
_NODE_ID_PATCH: str = (
    "import json,re,sys,pathlib;"
    "p=pathlib.Path(sys.argv[1]);"
    "t=p.read_text() if p.exists() else '';"
    "v=json.dumps(sys.argv[2]);"
    r"n,c=re.subn(r'(?m)^#?\s*node_id:.*$','node_id: '+v,t,count=1);"
    "p.write_text(n if c else (t.rstrip(chr(10))+'\\nnode_id: '+v+'\\n'))"
)


def _set_remote_node_id(host: HostConfig) -> None:
    """Bake the operator-chosen node_id into the node host's node.yaml.

    node_id is the node's stable bootstrap identity and the key for its central
    record (``configs/nodes/<node_id>.yaml``). Only runs for hosts that run the
    node service with an explicit ``node_id`` set; otherwise the node
    self-generates a uuid on first run (still dashboard-managed, just opaque).
    """
    if "node" not in host.services or not host.node_id:
        return
    cfg = f"{host.install_path}/configs/node.yaml"
    code = shlex.quote(_NODE_ID_PATCH)
    r = _ssh(
        host,
        f"{shlex.quote(host.python_bin)} -c {code} {shlex.quote(cfg)} {shlex.quote(host.node_id)}",
        check=False,
    )
    if r.returncode == 0:
        _ok(f"node_id set: {host.node_id}")
    else:
        _warn(f"could not set node_id on {host.name} (node will self-generate one)")


def _seed_central_config(host: HostConfig, local_path: Path, *, reseed: bool) -> None:
    """Seed the server's central, dashboard-owned override store from the operator tree.

    ``configs/nodes/`` and ``configs/services/`` are read by the server (via
    ``kenzy_data_root``) and edited live from the dashboard. We copy operator-
    authored files there with ``--ignore-existing`` so a re-deploy never clobbers
    a dashboard edit; ``reseed`` drops that flag to force the operator's values
    back. Only the server host holds the central store, so this is a no-op
    elsewhere.
    """
    if "server" not in host.services:
        return
    if host.local and local_path.resolve() == Path(host.install_path).resolve():
        return  # operator tree IS the live store — nothing to copy

    flags = ["rsync", "-az", "--info=progress2"]
    # seed: copy only files the server doesn't have (dashboard edits win).
    # reseed: force the operator's values back even if size+mtime match (--ignore-times),
    # otherwise a same-size edit could be skipped by rsync's quick-check.
    flags.append("--ignore-times" if reseed else "--ignore-existing")

    for sub in ("configs/nodes", "configs/services"):
        src = local_path / sub
        if not src.is_dir() or not any(src.iterdir()):
            continue
        dst = f"{host.install_path}/{sub}"
        _run(host, f"mkdir -p {shlex.quote(dst)}", check=False)
        dest = f"{dst}/" if host.local else f"{host.ssh_user}@{host.address}:{dst}/"
        r = subprocess.run([*flags, f"{src}/", dest], text=True)
        if r.returncode == 0:
            _ok(f"central config seeded: {sub}" + ("" if reseed else " (kept existing)"))
        else:
            _err(f"central config seed failed for {sub}")


def _rsync_path(
    host: HostConfig, local_path: Path, subpath: str, *, excludes: list[str] | None = None
) -> bool:
    """Sync a file or directory from the project root to the remote host.

    ``excludes`` are rsync patterns (relative to ``subpath``) kept out of the
    transfer; with ``--delete`` they are also protected from deletion on the
    receiver, so the central store can be excluded without being removed.
    """
    src = local_path / subpath
    if not src.exists():
        _warn(f"sync path does not exist locally, skipping: {subpath}")
        return True

    if host.local and local_path.resolve() == Path(host.install_path).resolve():
        _info(f"install_path matches project root — skipping sync for {subpath}")
        return True

    dst = f"{host.install_path}/{subpath}"

    exc: list[str] = []
    for e in excludes or []:
        exc += ["--exclude", e]

    if src.is_dir():
        _run(host, f"mkdir -p {shlex.quote(dst)}", check=False)
        cmd = ["rsync", "-az", "--delete", "--info=progress2", *exc, f"{src}/", f"{dst}/"]
    else:
        parent = str(Path(subpath).parent)
        if parent and parent != ".":
            _run(host, f"mkdir -p {shlex.quote(f'{host.install_path}/{parent}')}", check=False)
        cmd = ["rsync", "-az", "--info=progress2", *exc, str(src), dst]

    if not host.local:
        # Rewrite the destination as user@host:path for the remote case.
        cmd[-1] = f"{host.ssh_user}@{host.address}:{dst}"
        if src.is_dir():
            cmd[-2] = f"{src}/"

    result = subprocess.run(cmd, text=True)
    if result.returncode != 0:
        _err(f"rsync failed for {subpath}")
        return False
    return True


# ---------------------------------------------------------------------------
# Systemd unit generation
# ---------------------------------------------------------------------------


def _unit_name(service: str) -> str:
    return f"kenzy-{service}.service"


def _unit_content(service: str, host: HostConfig) -> str:
    info = SERVICE_INFO[service]
    # Pull-mode services run arg-less so they fetch their config from the server
    # (serviceboot) and stay dashboard-managed; they also order after the server
    # (no-op when kenzy-server isn't on this host). node/server pass a local config.
    env_lines = ""
    if service in _PULL_SERVICES:
        exec_start = f"{host.venv_path}/bin/{info['script']}"
        after_server = "After=kenzy-server.service\n"
        # Point pull-mode services straight at the server (auto-derived from the
        # fleet) so config-pull + auto-registration don't depend on mDNS; and bind
        # them on all interfaces when the operator asked for it (--listen-all).
        if host.server_url:
            env_lines += f"Environment=KENZY_SERVER_URL={host.server_url}\n"
        if host.listen_all:
            env_lines += "Environment=KENZY_BIND=0.0.0.0\n"
    else:
        exec_start = f"{host.venv_path}/bin/{info['script']} {host.install_path}/{info['config']}"
        after_server = ""
    return (
        f"[Unit]\n"
        f"Description={info['desc']}\n"
        f"After=network.target\n"
        f"Wants=network.target\n"
        f"{after_server}"
        f"\n"
        f"[Service]\n"
        f"Type=simple\n"
        f"User={host.ssh_user}\n"
        f"WorkingDirectory={host.install_path}\n"
        f"ExecStart={exec_start}\n"
        f"{env_lines}"
        f"Restart=on-failure\n"
        f"RestartSec=10\n"
        f"EnvironmentFile=-{host.install_path}/.env\n"
        f"\n"
        f"[Install]\n"
        f"WantedBy=multi-user.target\n"
    )


def _write_units(host: HostConfig) -> bool:
    """Write systemd unit files for all services on this host."""
    ok = True
    for svc in host.services:
        unit = _unit_name(svc)
        content = _unit_content(svc, host)
        tmp = f"/tmp/{unit}"

        # Write to tmp via stdin, then sudo-move to systemd directory.
        r = _ssh(host, f"cat > {shlex.quote(tmp)}", stdin=content, check=True)
        if r.returncode != 0:
            _err(f"failed to write unit: {unit}")
            ok = False
            continue

        r = _ssh(host, f"mv {shlex.quote(tmp)} /etc/systemd/system/{unit}", sudo=True)
        if r.returncode != 0:
            _err(f"failed to install unit: {unit}")
            ok = False

    r = _ssh(host, "systemctl daemon-reload", sudo=True)
    if r.returncode != 0:
        _err("daemon-reload failed")
        return False

    return ok


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _effective_yaml(host: HostConfig, filename: str, local_path: Path) -> dict[str, Any]:
    """Return parsed YAML for a service config, preferring the host overlay if present."""
    overlay = local_path / "configs" / "hosts" / host.name / filename
    base = local_path / "configs" / filename
    path = overlay if overlay.exists() else base
    if not path.exists():
        return {}
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


def _pip_extras(host: HostConfig, local_path: Path) -> str:
    """Build the pip extras string for this host.

    Runnable services + any explicit ``extras:`` (and non-service entries in
    ``services:``), plus ``kokoro`` auto-added when the TTS provider is kokoro.
    """
    extras: list[str] = [*host.services, *host.extras]

    if "tts" in host.services and "kokoro" not in extras:
        # Prefer the central store (configs/services/tts.yaml); fall back to legacy.
        central = local_path / "configs" / "services" / "tts.yaml"
        tts_cfg = (
            (yaml.safe_load(central.read_text()) or {})
            if central.exists()
            else _effective_yaml(host, "tts.yaml", local_path)
        )
        if str(tts_cfg.get("provider", "openai")).lower() == "kokoro":
            extras.append("kokoro")
            _info("tts provider=kokoro — adding kokoro extra")

    return ",".join(dict.fromkeys(extras))  # dedup, preserve order


def _pip_target(
    host: HostConfig, extras: str, *, upgrade: bool, constraints: str | None = None
) -> str:
    """Build the pip install target for this host's install mode.

    - source: editable install of the rsynced tree (``-e '<path>[extras]'``).
    - pypi:   ``'kenzy[extras]'`` pinned to ``==version`` or floored at ``>=3.0.0``
              (so the legacy 2.x monolith is never resolved); ``-U`` on upgrade.

    ``constraints`` is the remote path to a pip constraints file; when set it's passed
    with ``-c`` so operator pins are honored on install and upgrade (both modes).
    """
    c = f"-c '{constraints}' " if constraints else ""
    if host.install_mode == "pypi":
        spec = f"kenzy[{extras}]" + (f"=={host.version}" if host.version else ">=3.0.0")
        return f"{c}{'-U ' if upgrade else ''}'{spec}'"
    return f"{c}-e '{host.install_path}[{extras}]'"


def _sync_tree(host: HostConfig, local_path: Path, *, reseed: bool = False) -> bool:
    """Transfer what the host needs: full source (source mode) or just configs
    (pypi mode, code comes from PyPI), then per-host sync paths and overlays.

    The central store (configs/nodes, configs/services) is excluded from these
    syncs and seeded separately (seed-don't-clobber) so dashboard edits survive.
    """
    if host.install_mode == "pypi":
        _info("pypi mode: code from PyPI, syncing configs only…")
        # Keep the dashboard-owned central store out of the --delete configs sync.
        if not _rsync_path(host, local_path, "configs", excludes=["nodes/", "services/"]):
            return False
        _ok("configs synced")
    else:
        _info("syncing source…")
        if not _rsync(host, local_path):
            return False
        _ok("source synced")

    for subpath in host.sync:
        _info(f"syncing {subpath}…")
        if _rsync_path(host, local_path, subpath):
            _ok(f"{subpath} synced")

    _rsync_host_configs(host, local_path)
    _set_remote_node_id(host)
    _seed_central_config(host, local_path, reseed=reseed)
    return True


def _push_file(host: HostConfig, content: str, remote_path: str) -> bool:
    """Write ``content`` to ``remote_path`` on the host (local copy or over SSH)."""
    _run(host, f"mkdir -p {shlex.quote(str(Path(remote_path).parent))}", check=False)
    r = _ssh(host, f"cat > {shlex.quote(remote_path)}", stdin=content, check=True)
    return r.returncode == 0


def _resolve_constraints(host: HostConfig, local_path: Path) -> Path | None:
    """The local constraints file for this host: an explicit ``constraints:`` in
    deploy.yaml (relative to the config-root or absolute), else an auto-detected
    ``constraints.txt`` at the config-root. None if neither exists."""
    rel = host.constraints or "constraints.txt"
    cfile = local_path / rel
    return cfile if cfile.is_file() else None


def _provision(host: HostConfig, local_path: Path, *, upgrade: bool, reseed: bool = False) -> bool:
    """Shared install/upgrade body: sync, venv, pip, host pip packages."""
    if not _sync_tree(host, local_path, reseed=reseed):
        return False
    if not _ensure_venv(host):
        return False

    extras = _pip_extras(host, local_path)
    _maybe_install_cpu_torch(host, extras)

    # Push the operator constraints file (version pins) and pass it with -c so an
    # upgrade can't silently move a pin — same pattern as the per-user install path.
    constraints_remote: str | None = None
    cfile = _resolve_constraints(host, local_path)
    if cfile is not None:
        constraints_remote = f"{host.install_path}/constraints.txt"
        if not _push_file(host, cfile.read_text(), constraints_remote):
            _err("failed to push constraints file")
            return False
        _info(f"constraints: {cfile.name}")

    target = _pip_target(host, extras, upgrade=upgrade, constraints=constraints_remote)
    _info(f"pip install {target}…")
    r = _ssh(host, f"{shlex.quote(host.venv_path)}/bin/pip install -q {target}")
    if r.returncode != 0:
        _err("pip install failed")
        return False
    _ok("packages updated" if upgrade else "packages installed")

    _apply_pip_packages(host)
    return True


_TORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"


def _needs_torch(extras: str) -> bool:
    """Return True if any selected extra pulls in PyTorch."""
    return any(e in extras.split(",") for e in ("speaker", "kokoro"))


def _maybe_install_cpu_torch(host: HostConfig, extras: str) -> None:
    """Pre-install torch/torchaudio from the correct index for this host.

    pip defaults to the newest CUDA-enabled torch wheel, which may not match
    the host's installed CUDA runtime version.  Installing torch first from
    the right index satisfies the torch>=X.Y.Z constraint so the subsequent
    pip install -e '.[extras]' leaves it untouched.

    Priority:
      1. host.torch_index_url explicitly set in deploy.yaml — use it directly.
      2. No GPU detected (nvidia-smi fails) — use CPU index.
      3. GPU present but no index specified — let pip pick (user's CUDA must
         match the default PyPI wheel; if not, set torch_index_url).
    """
    if not _needs_torch(extras):
        return

    if host.torch_index_url:
        index_url = host.torch_index_url
        _info(f"torch index: {index_url} (from deploy.yaml)")
    else:
        r = _run(host, "nvidia-smi", check=False)
        if r.returncode == 0:
            _info("GPU detected, no torch_index_url set — using PyPI default")
            _info("  If you see libcudart errors, set torch_index_url in deploy.yaml")
            return
        index_url = _TORCH_CPU_INDEX
        _info("No GPU detected — pre-installing CPU-only torch…")

    r = _run(
        host,
        f"{shlex.quote(host.venv_path)}/bin/pip install -q --force-reinstall --no-deps "
        f"torch torchaudio --index-url {shlex.quote(index_url)}",
    )
    if r.returncode == 0:
        _ok("torch installed")
    else:
        _warn("torch pre-install failed — default PyPI wheel will be used")


def _apply_pip_packages(host: HostConfig) -> None:
    """Install any host-specific pip packages listed under pip_packages in deploy.yaml."""
    if not host.pip_packages:
        return
    packages = " ".join(shlex.quote(p) for p in host.pip_packages)
    _info(f"pip install (host-specific): {' '.join(host.pip_packages)}")
    r = _run(host, f"{shlex.quote(host.venv_path)}/bin/pip install -q {packages}")
    if r.returncode == 0:
        _ok("host-specific packages installed")
    else:
        _warn("host-specific pip install failed — check versions manually")


def _ensure_venv(host: HostConfig) -> bool:
    """Create the virtualenv if it doesn't already exist. Returns True on success."""
    pip = f"{host.venv_path}/bin/pip"
    r = _ssh(host, f"test -x {shlex.quote(pip)}", check=False)
    if r.returncode == 0:
        return True  # already exists
    _info(f"virtualenv not found — creating with {host.python_bin}…")
    r = _ssh(host, f"{shlex.quote(host.python_bin)} -m venv {shlex.quote(host.venv_path)}")
    if r.returncode != 0:
        _err("virtualenv creation failed")
        return False
    _ok("virtualenv created")
    return True


def cmd_init(hosts: list[HostConfig]) -> None:
    """Install OS-level dependencies and create the install directory."""
    for host in hosts:
        _header(host.name, f"init  {host.address}")

        packages = sorted(
            set(APT_BASE + [pkg for svc in host.services for pkg in APT_EXTRA.get(svc, [])])
        )
        _info(f"apt packages: {' '.join(packages)}")

        r = _ssh(host, f"apt-get install -y {' '.join(packages)}", sudo=True)
        if r.returncode != 0:
            _err("apt install failed — skipping this host")
            continue
        _ok("apt packages installed")

        r = _ssh(host, f"mkdir -p {shlex.quote(host.install_path)}", sudo=True)
        if r.returncode != 0:
            _err("failed to create install directory")
            continue
        r = _ssh(
            host,
            f"chown {shlex.quote(host.ssh_user)}:{shlex.quote(host.ssh_user)} "
            f"{shlex.quote(host.install_path)}",
            sudo=True,
        )
        if r.returncode == 0:
            _ok(f"install directory ready: {host.install_path}")
        else:
            _err("failed to set ownership on install directory")


def cmd_install(hosts: list[HostConfig], local_path: Path, *, reseed: bool = False) -> None:
    """First-time full install: sync, venv, pip, models, systemd."""
    for host in hosts:
        _header(
            host.name,
            f"install  {host.address}  services={host.services}  mode={host.install_mode}",
        )

        if not _provision(host, local_path, upgrade=False, reseed=reseed):
            continue

        _info("downloading models (kenzy-setup)…")
        r = _ssh(
            host,
            f"cd {host.install_path} && "
            f"{host.venv_path}/bin/kenzy-setup {host.install_path}/configs/speaker.yaml",
            check=False,
        )
        if r.returncode == 0:
            _ok("models downloaded")
        else:
            _warn("kenzy-setup had warnings (models may already be present or not applicable)")

        _info("writing systemd unit files…")
        if not _write_units(host):
            continue

        for svc in host.services:
            unit = _unit_name(svc)
            r = _ssh(host, f"systemctl enable --now {unit}", sudo=True)
            if r.returncode == 0:
                _ok(f"enabled + started {unit}")
            else:
                _err(f"enable failed: {unit}")


def cmd_upgrade(hosts: list[HostConfig], local_path: Path, *, reseed: bool = False) -> None:
    """Sync, update packages, re-write units, restart services."""
    for host in hosts:
        _header(host.name, f"upgrade  {host.address}  mode={host.install_mode}")

        if not _provision(host, local_path, upgrade=True, reseed=reseed):
            continue

        _info("updating systemd unit files…")
        _write_units(host)

        for svc in host.services:
            unit = _unit_name(svc)
            r = _ssh(host, f"systemctl restart {unit}", sudo=True)
            if r.returncode == 0:
                _ok(f"restarted {unit}")
            else:
                _err(f"restart failed: {unit}")


def cmd_status(hosts: list[HostConfig]) -> None:
    """Print systemctl status for all services on each host."""
    for host in hosts:
        _header(host.name, f"status  {host.address}")
        units = " ".join(_unit_name(s) for s in host.services)
        r = _ssh(
            host,
            f"systemctl status {units} --no-pager -l",
            check=False,
        )
        for line in r.stdout.splitlines():
            print(f"  {line}")


def cmd_service_action(action: str, service: str, hosts: list[HostConfig]) -> None:
    """Start, stop, or restart a named service on one or more hosts."""
    for host in hosts:
        if service not in host.services:
            continue
        unit = _unit_name(service)
        _header(host.name, f"{action} {unit}")
        r = _ssh(host, f"systemctl {action} {unit}", sudo=True)
        if r.returncode == 0:
            _ok(f"{action}ed {unit}")
        else:
            _err(f"{action} failed: {unit}")


def cmd_logs(service: str, host: HostConfig) -> None:
    """Tail journald logs for a service (interactive, Ctrl+C to exit)."""
    unit = _unit_name(service)
    _header(host.name, f"logs: {unit}  (Ctrl+C to exit)")
    _ssh_interactive(host, f"sudo journalctl -u {unit} -f --no-pager")


# Critical/shallow paths we refuse to ``rm -rf`` even if a deploy.yaml is
# misconfigured (e.g. an empty or root install_path/venv_path).
_UNSAFE_REMOVE: frozenset[str] = frozenset(
    {
        "",
        "/",
        "/root",
        "/home",
        "/usr",
        "/etc",
        "/var",
        "/opt",
        "/bin",
        "/sbin",
        "/boot",
        "/lib",
        "/lib64",
        "/srv",
        "/tmp",
        "/mnt",
        "/media",
    }
)


def _safe_to_remove(path: str) -> bool:
    """Guard against ``rm -rf`` on a dangerously shallow or critical path.

    Requires at least two path components below root (e.g. ``/opt/kenzy``) so a
    misconfigured ``install_path``/``venv_path`` can't wipe a system directory.
    """
    p = path.rstrip("/")
    if p in _UNSAFE_REMOVE:
        return False
    return len([seg for seg in p.split("/") if seg]) >= 2


def cmd_uninstall(hosts: list[HostConfig], *, purge: bool, assume_yes: bool) -> None:
    """Stop + disable services, remove systemd units, and delete the venv.

    The inverse of install. With ``purge`` it also deletes the install directory
    (configs, ``.env``, models, data); without it the install dir is kept so a
    reinstall preserves state. Asks for confirmation per host unless ``assume_yes``.
    """
    for host in hosts:
        _header(
            host.name,
            f"uninstall  {host.address}  services={host.services}" + ("  (purge)" if purge else ""),
        )

        units = [_unit_name(s) for s in host.services]
        _info(f"systemd units: {', '.join(units) if units else '(none)'}")
        _info(f"venv: {host.venv_path}")
        if purge:
            _warn(f"PURGE: install dir will be deleted: {host.install_path}")
        else:
            _info(f"install dir kept (configs/.env/models): {host.install_path}")

        if not assume_yes:
            try:
                ans = input(f"  Remove the above on {host.name!r}? type 'yes' to confirm: ")
            except EOFError:
                ans = ""
            if ans.strip().lower() != "yes":
                _warn("skipped (not confirmed)")
                continue

        # 1. Stop + disable units.
        for unit in units:
            r = _ssh(host, f"systemctl disable --now {unit}", sudo=True, check=False)
            if r.returncode == 0:
                _ok(f"stopped + disabled {unit}")
            else:
                _warn(f"could not disable {unit} (already removed?)")

        # 2. Remove unit files, then daemon-reload.
        if units:
            paths = " ".join(f"/etc/systemd/system/{u}" for u in units)
            _ssh(host, f"rm -f {paths}", sudo=True, check=False)
            _ssh(host, "systemctl daemon-reload", sudo=True, check=False)
            _ok("removed unit files")

        # 3. Remove the venv.
        if _safe_to_remove(host.venv_path):
            r = _ssh(host, f"rm -rf {shlex.quote(host.venv_path)}", sudo=True, check=False)
            if r.returncode == 0:
                _ok(f"removed venv: {host.venv_path}")
            else:
                _err(f"failed to remove venv: {host.venv_path}")
        else:
            _err(f"refusing to remove unsafe venv path: {host.venv_path!r}")

        # 4. Optionally purge the install directory.
        if purge:
            if _safe_to_remove(host.install_path):
                r = _ssh(host, f"rm -rf {shlex.quote(host.install_path)}", sudo=True, check=False)
                if r.returncode == 0:
                    _ok(f"purged install dir: {host.install_path}")
                else:
                    _err(f"failed to purge install dir: {host.install_path}")
            else:
                _err(f"refusing to purge unsafe install path: {host.install_path!r}")
        else:
            _info("re-run with --purge to also delete the install dir")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _load_hosts(
    config_path: str,
    *,
    force_source: bool = False,
    version_override: str | None = None,
    listen_all: bool = False,
) -> list[HostConfig]:
    with open(config_path) as fh:
        raw: dict[str, Any] = yaml.safe_load(fh)

    defaults: dict[str, Any] = raw.get("defaults", {})
    # install_mode/version may be set at top level or under defaults; per-host wins.
    default_mode = str(raw.get("install_mode", defaults.get("install_mode", "source")))
    default_version = raw.get("version", defaults.get("version"))

    # Derive the URL pull-mode services use to reach the server: an explicit
    # `server_url:` wins, else it's built from the host running the `server` service
    # (loopback for a co-located service, the server host's address otherwise) on
    # `server_port:` (default 8765). None when no host runs the server (→ mDNS).
    explicit_server_url = raw.get("server_url") or defaults.get("server_url") or None
    server_port = int(raw.get("server_port", 8765))
    server_addr = next(
        (
            str(h["address"])
            for h in raw.get("hosts", {}).values()
            if "server" in (h.get("services") or [])
        ),
        None,
    )
    service_sync: dict[str, list[str]] = {
        k: [str(p) for p in v] for k, v in raw.get("service_sync", {}).items()
    }
    hosts: list[HostConfig] = []

    def _d(hcfg: dict[str, Any], key: str, fallback: str) -> str:
        return str(hcfg.get(key, defaults.get(key, fallback)))

    for name, hcfg in raw.get("hosts", {}).items():
        install_path = _d(hcfg, "install_path", "/opt/kenzy")

        # Merge sync paths: defaults → service-derived → host-specific, deduplicated.
        seen: set[str] = set()
        sync_paths: list[str] = []
        for p in defaults.get("sync", []):
            sp = str(p)
            if sp not in seen:
                sync_paths.append(sp)
                seen.add(sp)
        for svc in hcfg.get("services", []):
            for p in service_sync.get(svc, []):
                if p not in seen:
                    sync_paths.append(p)
                    seen.add(p)
        for p in hcfg.get("sync", []):
            sp = str(p)
            if sp not in seen:
                sync_paths.append(sp)
                seen.add(sp)

        torch_url = hcfg.get("torch_index_url") or defaults.get("torch_index_url") or None

        pip_pkgs: list[str] = list(defaults.get("pip_packages", []))
        for p in hcfg.get("pip_packages", []):
            if p not in pip_pkgs:
                pip_pkgs.append(p)

        mode = "source" if force_source else str(hcfg.get("install_mode", default_mode))
        ver = version_override or hcfg.get("version", default_version)

        # Split `services:` into runnable services (get a systemd unit) and anything
        # else (treated as a pip extra — e.g. kokoro, mqtt), merged with `extras:`.
        raw_services = list(hcfg.get("services", []))
        svcs = [s for s in raw_services if s in SERVICE_INFO]
        host_extras: list[str] = [s for s in raw_services if s not in SERVICE_INFO]
        for e in [*defaults.get("extras", []), *hcfg.get("extras", [])]:
            if str(e) not in host_extras:
                host_extras.append(str(e))

        if explicit_server_url:
            surl: str | None = str(explicit_server_url)
        elif server_addr is not None:
            # Loopback when this host also runs the server; else the server's address.
            reach = "127.0.0.1" if "server" in svcs else server_addr
            surl = f"ws://{reach}:{server_port}"
        else:
            surl = None

        hosts.append(
            HostConfig(
                name=name,
                address=str(hcfg["address"]),
                ssh_user=_d(hcfg, "ssh_user", "pi"),
                install_path=install_path,
                venv_path=_d(hcfg, "venv_path", f"{install_path}/.venv"),
                python_bin=_d(hcfg, "python_bin", "python3"),
                local=bool(hcfg.get("local", defaults.get("local", False))),
                services=svcs,
                sync=sync_paths,
                torch_index_url=str(torch_url) if torch_url else None,
                pip_packages=pip_pkgs,
                install_mode=mode,
                version=str(ver) if ver else None,
                constraints=(hcfg.get("constraints") or defaults.get("constraints") or None),
                node_id=(str(hcfg["node_id"]) if hcfg.get("node_id") else None),
                server_url=surl,
                listen_all=listen_all,
                extras=host_extras,
            )
        )

    return hosts


def _select(all_hosts: list[HostConfig], name: str | None) -> list[HostConfig]:
    if name is None:
        return all_hosts
    matches = [h for h in all_hosts if h.name == name]
    if not matches:
        print(_red(f"Unknown host: {name!r}"), file=sys.stderr)
        sys.exit(1)
    return matches


def _config_root(config_path: str) -> Path:
    """Operational/source root holding configs/, skills/, data/ — the rsync base.

    Rooted on the deploy.yaml location (config-root) rather than pyproject.toml,
    so pypi-mode deploys work from an operational tree with no source checkout.
    A ``<root>/configs/deploy.yaml`` layout yields ``<root>``; otherwise falls
    back to a pyproject walk, then the file's own directory.
    """
    p = Path(config_path).resolve()
    if p.parent.name == "configs":
        return p.parent.parent
    for path in [p.parent, *p.parent.parents]:
        if (path / "pyproject.toml").exists():
            return path
    return p.parent


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="kenzy-deploy",
        description="Remote deployment and management for kenzy services.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default="configs/deploy.yaml",
        metavar="PATH",
        help="Deploy config file (default: configs/deploy.yaml)",
    )
    parser.add_argument(
        "--host",
        default=None,
        metavar="NAME",
        help="Target a single host by name (default: all hosts)",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Force source (rsync + editable) install mode, overriding deploy.yaml",
    )
    parser.add_argument(
        "--version",
        default=None,
        metavar="V",
        help="Override the PyPI version to install (pypi mode), e.g. 3.1.0",
    )
    parser.add_argument(
        "--reseed",
        action="store_true",
        help="On install/upgrade, overwrite the server's central config store "
        "(configs/nodes, configs/services) from the operator tree — discards "
        "dashboard edits. Default: seed only files that don't exist yet.",
    )
    parser.add_argument(
        "--listen-all",
        action="store_true",
        help="Bind backend services (stt/tts/llm/speaker) to 0.0.0.0 instead of "
        "127.0.0.1, so they're reachable across the network (needed for multi-host). "
        "Set a KENZY_SERVICE_TOKEN too, since this exposes them on the LAN.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Install OS dependencies + create install directory")
    sub.add_parser("install", help="Full first-time install (sync, venv, pip, systemd)")
    sub.add_parser("upgrade", help="Sync source, update packages, restart services")
    sub.add_parser("status", help="Show service status on target host(s)")

    for action in ("start", "stop", "restart"):
        p = sub.add_parser(action, help=f"{action.capitalize()} a service")
        p.add_argument("service", choices=sorted(SERVICE_INFO))

    logs_p = sub.add_parser("logs", help="Tail service logs (requires --host)")
    logs_p.add_argument("service", choices=sorted(SERVICE_INFO))

    unin_p = sub.add_parser(
        "uninstall",
        help="Stop + disable services, remove units and venv "
        "(--purge also deletes the install dir)",
    )
    unin_p.add_argument(
        "--purge",
        action="store_true",
        help="Also delete the install directory (configs, .env, models, data)",
    )
    unin_p.add_argument(
        "--yes",
        action="store_true",
        help="Skip the per-host confirmation prompt",
    )

    args = parser.parse_args()

    all_hosts = _load_hosts(
        args.config,
        force_source=args.local,
        version_override=args.version,
        listen_all=args.listen_all,
    )
    local_path = _config_root(args.config)

    if args.command == "logs":
        if not args.host:
            print(_red("--host is required for logs"), file=sys.stderr)
            sys.exit(1)
        host = _select(all_hosts, args.host)[0]
        cmd_logs(args.service, host)
        return

    hosts = _select(all_hosts, args.host)

    match args.command:
        case "init":
            cmd_init(hosts)
        case "install":
            cmd_install(hosts, local_path, reseed=args.reseed)
        case "upgrade":
            cmd_upgrade(hosts, local_path, reseed=args.reseed)
        case "status":
            cmd_status(hosts)
        case "start" | "stop" | "restart":
            cmd_service_action(args.command, args.service, hosts)
        case "uninstall":
            cmd_uninstall(hosts, purge=args.purge, assume_yes=args.yes)


if __name__ == "__main__":
    main()
