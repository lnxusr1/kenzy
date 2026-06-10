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
"""

from __future__ import annotations

import argparse
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


def _bold(t: str) -> str:  return _c("\033[1m", t)
def _green(t: str) -> str: return _c("\033[32m", t)
def _red(t: str) -> str:   return _c("\033[31m", t)
def _yellow(t: str) -> str: return _c("\033[33m", t)
def _cyan(t: str) -> str:  return _c("\033[36m", t)
def _dim(t: str) -> str:   return _c("\033[2m", t)


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
    "node":    {
        "script": "kenzy-node",
        "config": "configs/node.yaml",
        "desc":   "Kenzy Node (wake word + audio capture)",
    },
    "server":  {
        "script": "kenzy-server",
        "config": "configs/server.yaml",
        "desc":   "Kenzy Server (WebSocket hub + pipeline)",
    },
    "stt":     {
        "script": "kenzy-stt",
        "config": "configs/stt.yaml",
        "desc":   "Kenzy STT (speech-to-text)",
    },
    "tts":     {
        "script": "kenzy-tts",
        "config": "configs/tts.yaml",
        "desc":   "Kenzy TTS (text-to-speech)",
    },
    "llm":     {
        "script": "kenzy-llm",
        "config": "configs/llm.yaml",
        "desc":   "Kenzy LLM (language model)",
    },
    "speaker": {
        "script": "kenzy-speaker",
        "config": "configs/speaker.yaml",
        "desc":   "Kenzy Speaker (voice identification)",
    },
}

# OS packages required per service (beyond the base set).
APT_BASE: list[str] = ["python3", "python3-pip", "python3-venv", "git", "rsync"]

APT_EXTRA: dict[str, list[str]] = {
    "node":    ["libportaudio2", "portaudio19-dev", "python3-dev"],
    "server":  [],
    "stt":     ["ffmpeg", "libgomp1"],
    "tts":     ["espeak-ng"],
    "llm":     [],
    "speaker": ["libportaudio2", "portaudio19-dev", "python3-dev", "libgomp1"],
}

# Files / dirs excluded from the base rsync to the remote.
# Extra paths (skills/, data/, models/, etc.) are synced separately per host
# based on service_sync in deploy.yaml.
RSYNC_EXCLUDES: list[str] = [
    # Match at any depth — intentionally unanchored.
    "__pycache__/", "*.pyc", ".mypy_cache/", ".ruff_cache/", "*.egg-info/",
    # Root-anchored: only exclude the top-level directory, not same-named
    # subdirectories inside the package (e.g. src/kenzy/llm/skills/).
    "/.venv/",
    "/skills/", "/data/", "/models/",
    "/.env",         # secrets stay on each host
]

# ---------------------------------------------------------------------------
# Host config
# ---------------------------------------------------------------------------


@dataclass
class HostConfig:
    name:            str
    address:         str
    ssh_user:        str
    install_path:    str
    venv_path:       str
    python_bin:      str
    local:           bool = False   # run commands directly instead of over SSH
    services:        list[str] = field(default_factory=list)
    sync:            list[str] = field(default_factory=list)  # extra paths to sync
    torch_index_url: str | None = None  # PyTorch wheel index; None = auto-detect
    pip_packages:    list[str] = field(default_factory=list)  # extra pip installs after main


# ---------------------------------------------------------------------------
# SSH / rsync helpers
# ---------------------------------------------------------------------------

_SSH_OPTS: list[str] = [
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
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
            cmd, shell=True, input=stdin, capture_output=True, text=True,
        )
    else:
        result = subprocess.run(
            ["ssh", *_SSH_OPTS, f"{host.ssh_user}@{host.address}", cmd],
            input=stdin, capture_output=True, text=True,
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
                "rsync", "-az", "--delete",
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
                "rsync", "-az", "--delete",
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
        cmd = ["rsync", "-az", "--info=progress2",
               f"{overlay}/", f"{host.ssh_user}@{host.address}:{dst}/"]

    result = subprocess.run(cmd, text=True)
    if result.returncode != 0:
        _err(f"host config overlay failed for {host.name}")
    else:
        _ok(f"host config overlay applied ({overlay.relative_to(local_path)})")


def _rsync_path(host: HostConfig, local_path: Path, subpath: str) -> bool:
    """Sync a file or directory from the project root to the remote host."""
    src = local_path / subpath
    if not src.exists():
        _warn(f"sync path does not exist locally, skipping: {subpath}")
        return True

    if host.local and local_path.resolve() == Path(host.install_path).resolve():
        _info(f"install_path matches project root — skipping sync for {subpath}")
        return True

    dst = f"{host.install_path}/{subpath}"

    if src.is_dir():
        _run(host, f"mkdir -p {dst}", check=False)
        cmd = ["rsync", "-az", "--delete", "--info=progress2", f"{src}/", f"{dst}/"]
    else:
        parent = str(Path(subpath).parent)
        if parent and parent != ".":
            _run(host, f"mkdir -p {host.install_path}/{parent}", check=False)
        cmd = ["rsync", "-az", "--info=progress2", str(src), dst]

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
    return (
        f"[Unit]\n"
        f"Description={info['desc']}\n"
        f"After=network.target\n"
        f"Wants=network.target\n"
        f"\n"
        f"[Service]\n"
        f"Type=simple\n"
        f"User={host.ssh_user}\n"
        f"WorkingDirectory={host.install_path}\n"
        f"ExecStart={host.venv_path}/bin/{info['script']} "
        f"{host.install_path}/{info['config']}\n"
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
        r = _ssh(host, f"cat > {tmp}", stdin=content, check=True)
        if r.returncode != 0:
            _err(f"failed to write unit: {unit}")
            ok = False
            continue

        r = _ssh(host, f"mv {tmp} /etc/systemd/system/{unit}", sudo=True)
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
    base    = local_path / "configs" / filename
    path    = overlay if overlay.exists() else base
    if not path.exists():
        return {}
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


def _pip_extras(host: HostConfig, local_path: Path) -> str:
    """Build the pip extras string for this host, including optional add-ons."""
    extras: list[str] = list(host.services)

    if "tts" in host.services:
        tts_cfg = _effective_yaml(host, "tts.yaml", local_path)
        if str(tts_cfg.get("provider", "openai")).lower() == "kokoro":
            extras.append("kokoro")
            _info("tts.yaml provider=kokoro — adding kokoro extra")

    return ",".join(extras)


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
        f"{host.venv_path}/bin/pip install -q --force-reinstall --no-deps "
        f"torch torchaudio --index-url {index_url}",
    )
    if r.returncode == 0:
        _ok("torch installed")
    else:
        _warn("torch pre-install failed — default PyPI wheel will be used")


def _apply_pip_packages(host: HostConfig) -> None:
    """Install any host-specific pip packages listed under pip_packages in deploy.yaml."""
    if not host.pip_packages:
        return
    packages = " ".join(f"'{p}'" for p in host.pip_packages)
    _info(f"pip install (host-specific): {' '.join(host.pip_packages)}")
    r = _run(host, f"{host.venv_path}/bin/pip install -q {packages}")
    if r.returncode == 0:
        _ok("host-specific packages installed")
    else:
        _warn("host-specific pip install failed — check versions manually")


def _ensure_venv(host: HostConfig) -> bool:
    """Create the virtualenv if it doesn't already exist. Returns True on success."""
    pip = f"{host.venv_path}/bin/pip"
    r = _ssh(host, f"test -x {pip}", check=False)
    if r.returncode == 0:
        return True  # already exists
    _info(f"virtualenv not found — creating with {host.python_bin}…")
    r = _ssh(host, f"{host.python_bin} -m venv {host.venv_path}")
    if r.returncode != 0:
        _err("virtualenv creation failed")
        return False
    _ok("virtualenv created")
    return True


def cmd_init(hosts: list[HostConfig]) -> None:
    """Install OS-level dependencies and create the install directory."""
    for host in hosts:
        _header(host.name, f"init  {host.address}")

        packages = sorted(set(APT_BASE + [
            pkg
            for svc in host.services
            for pkg in APT_EXTRA.get(svc, [])
        ]))
        _info(f"apt packages: {' '.join(packages)}")

        r = _ssh(host, f"apt-get install -y {' '.join(packages)}", sudo=True)
        if r.returncode != 0:
            _err("apt install failed — skipping this host")
            continue
        _ok("apt packages installed")

        r = _ssh(host, f"mkdir -p {host.install_path}", sudo=True)
        if r.returncode != 0:
            _err("failed to create install directory")
            continue
        r = _ssh(host, f"chown {host.ssh_user}:{host.ssh_user} {host.install_path}", sudo=True)
        if r.returncode == 0:
            _ok(f"install directory ready: {host.install_path}")
        else:
            _err("failed to set ownership on install directory")


def cmd_install(hosts: list[HostConfig], local_path: Path) -> None:
    """First-time full install: rsync, venv, pip, systemd."""
    for host in hosts:
        _header(host.name, f"install  {host.address}  services={host.services}")

        _info("syncing source…")
        if not _rsync(host, local_path):
            continue
        _ok("source synced")

        for subpath in host.sync:
            _info(f"syncing {subpath}…")
            if _rsync_path(host, local_path, subpath):
                _ok(f"{subpath} synced")

        _rsync_host_configs(host, local_path)

        if not _ensure_venv(host):
            continue

        extras = _pip_extras(host, local_path)
        _maybe_install_cpu_torch(host, extras)

        _info(f"pip install -e '[{extras}]'…")
        r = _ssh(
            host,
            f"{host.venv_path}/bin/pip install -q -e '{host.install_path}[{extras}]'",
        )
        if r.returncode != 0:
            _err("pip install failed")
            continue
        _ok("packages installed")

        _apply_pip_packages(host)

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


def cmd_upgrade(hosts: list[HostConfig], local_path: Path) -> None:
    """Sync source, update packages, re-write units, restart services."""
    for host in hosts:
        _header(host.name, f"upgrade  {host.address}")

        _info("syncing source…")
        if not _rsync(host, local_path):
            continue
        _ok("source synced")

        for subpath in host.sync:
            _info(f"syncing {subpath}…")
            if _rsync_path(host, local_path, subpath):
                _ok(f"{subpath} synced")

        _rsync_host_configs(host, local_path)

        if not _ensure_venv(host):
            continue

        extras = _pip_extras(host, local_path)
        _maybe_install_cpu_torch(host, extras)

        _info(f"pip install -e '[{extras}]'…")
        r = _ssh(
            host,
            f"{host.venv_path}/bin/pip install -q -e '{host.install_path}[{extras}]'",
        )
        if r.returncode != 0:
            _err("pip install failed")
            continue
        _ok("packages updated")

        _apply_pip_packages(host)

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


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _load_hosts(config_path: str) -> list[HostConfig]:
    with open(config_path) as fh:
        raw: dict[str, Any] = yaml.safe_load(fh)

    defaults: dict[str, Any] = raw.get("defaults", {})
    service_sync: dict[str, list[str]] = {
        k: [str(p) for p in v]
        for k, v in raw.get("service_sync", {}).items()
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

        hosts.append(HostConfig(
            name=name,
            address=str(hcfg["address"]),
            ssh_user=_d(hcfg, "ssh_user", "pi"),
            install_path=install_path,
            venv_path=_d(hcfg, "venv_path", f"{install_path}/.venv"),
            python_bin=_d(hcfg, "python_bin", "python3"),
            local=bool(hcfg.get("local", defaults.get("local", False))),
            services=list(hcfg.get("services", [])),
            sync=sync_paths,
            torch_index_url=str(torch_url) if torch_url else None,
            pip_packages=pip_pkgs,
        ))

    return hosts


def _select(all_hosts: list[HostConfig], name: str | None) -> list[HostConfig]:
    if name is None:
        return all_hosts
    matches = [h for h in all_hosts if h.name == name]
    if not matches:
        print(_red(f"Unknown host: {name!r}"), file=sys.stderr)
        sys.exit(1)
    return matches


def _find_project_root() -> Path:
    """Walk up from CWD until pyproject.toml is found."""
    here = Path.cwd()
    for path in [here, *here.parents]:
        if (path / "pyproject.toml").exists():
            return path
    return here


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
        "--config", default="configs/deploy.yaml",
        metavar="PATH",
        help="Deploy config file (default: configs/deploy.yaml)",
    )
    parser.add_argument(
        "--host", default=None, metavar="NAME",
        help="Target a single host by name (default: all hosts)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init",    help="Install OS dependencies + create install directory")
    sub.add_parser("install", help="Full first-time install (sync, venv, pip, systemd)")
    sub.add_parser("upgrade", help="Sync source, update packages, restart services")
    sub.add_parser("status",  help="Show service status on target host(s)")

    for action in ("start", "stop", "restart"):
        p = sub.add_parser(action, help=f"{action.capitalize()} a service")
        p.add_argument("service", choices=sorted(SERVICE_INFO))

    logs_p = sub.add_parser("logs", help="Tail service logs (requires --host)")
    logs_p.add_argument("service", choices=sorted(SERVICE_INFO))

    args = parser.parse_args()

    all_hosts = _load_hosts(args.config)
    local_path = _find_project_root()

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
            cmd_install(hosts, local_path)
        case "upgrade":
            cmd_upgrade(hosts, local_path)
        case "status":
            cmd_status(hosts)
        case "start" | "stop" | "restart":
            cmd_service_action(args.command, args.service, hosts)


if __name__ == "__main__":
    main()
