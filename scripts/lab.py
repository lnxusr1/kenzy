#!/usr/bin/env python3
"""Drive the test lab: install Kenzy across real machines and check the result.

This is the tier the hosted CI matrix cannot reach. Containers prove that a
package resolves and a model loads; only real hosts prove that `systemd --user`
units come up, that a node discovers a server over mDNS and pulls its config,
and that a fleet survives a deploy. Two of those — `kenzy-deploy` and the
upgrade sweep — have never been tested anywhere, because they cannot exist on
one machine.

Run from the workstation, which holds SSH keys to every lab host. Nothing is
installed here and no host needs keys to any other.

    scripts/lab.py check                 # reachability + facts, changes nothing
    scripts/lab.py all                   # build -> install -> smoke -> fleet
    scripts/lab.py install --hosts vm1,vm2
    scripts/lab.py fleet                 # assert the roster on the server

Rolling back to the pre-install snapshots is deliberately NOT automated here:
it needs credentials on the hypervisor, which this script has no business
holding. Roll back from the Proxmox side, then re-run `all`.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO.parent / "kenzy-www" / "src" / "install.sh"
SMOKE = REPO / "scripts" / "ci_smoke.py"

VENV = ".local/share/kenzy/venv"       # install.sh's default
KHOME = ".config/kenzy"                # kenzy_home() default
SSH = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]


@dataclass
class Host:
    """One lab machine.

    `expect_model` is an assertion about what install.sh CHOOSES here, derived
    from the host's Python version — never an instruction to it. install.sh
    adds the `wakeword` extra only at <=3.11 (tflite); above that it installs
    openwakeword without its tflite dependency and the ONNX model is used.
    """

    fqdn: str
    profile: str                        # node | server | all
    net: int
    expect_model: str = "none"
    audio: bool = False
    pin_localhost: bool = False         # all-in-one: point the node at its own server
    canary: bool = False                # failure is reported, never fatal


HOSTS: dict[str, Host] = {
    # Net 1 — the fleet. Discovery is left ON here on purpose: mDNS is a real
    # code path and the one behind the 47-hour orphaned-node incident.
    "vm1": Host("vm1.lan", "server", 1),
    "vm2": Host("vm2.lan", "node", 1, expect_model="tflite"),
    "vm3": Host("vm3.lan", "node", 1, expect_model="tflite"),
    "pi-a": Host("pi-a.lan", "node", 1, expect_model="onnx", audio=True),
    "pi-b": Host("pi-b.lan", "node", 1, expect_model="onnx"),
    # Self-contained all-in-one hosts. Pinned to their own server so a
    # neighbour's broadcast can't claim them — the designed option, used as
    # designed, rather than a workaround for a defect.
    "vm4": Host("vm4.lan", "all", 2, expect_model="onnx", pin_localhost=True),
    "vm5": Host("vm5.lan", "all", 3, expect_model="onnx", pin_localhost=True),
}

SERVER = "vm1"
NET1_NODES = [n for n, h in HOSTS.items() if h.net == 1 and h.profile == "node"]


@dataclass
class Result:
    host: str
    stage: str
    ok: bool
    detail: str = ""


_results: list[Result] = []
_pending: list[Result] = []
_lock = threading.Lock()


def record(host: str, stage: str, ok: bool, detail: str = "") -> bool:
    with _lock:
        r = Result(host, stage, ok, detail)
        _results.append(r)
        _pending.append(r)
    return ok


def flush() -> None:
    """Print buffered results in inventory order.

    Workers finish out of order, so printing as we go scrambles the hosts —
    which matters when the whole point is scanning seven of them at a glance.
    """
    order = list(HOSTS) + ["local"]
    for r in sorted(_pending, key=lambda x: order.index(x.host) if x.host in order else 99):
        mark = "ok  " if r.ok else "FAIL"
        print(f"  {mark} {r.host:<6} {r.stage}{'  — ' + r.detail if r.detail and not r.ok else ''}")
    _pending.clear()


def ssh(host: str, cmd: str, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ssh", *SSH, HOSTS[host].fqdn, cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def scp(host: str, local: Path, remote: str) -> bool:
    r = subprocess.run(
        ["scp", *SSH, str(local), f"{HOSTS[host].fqdn}:{remote}"],
        capture_output=True, text=True, timeout=600,
    )
    return r.returncode == 0


def fan(hosts: list[str], fn) -> list[bool]:
    """Run fn(host) across hosts concurrently. Installs are IO-bound."""
    with ThreadPoolExecutor(max_workers=len(hosts) or 1) as pool:
        return list(pool.map(fn, hosts))


# --- stages -----------------------------------------------------------------

def stage_check(hosts: list[str]) -> bool:
    print("\n[check] reachability and facts")

    def one(h: str) -> bool:
        r = ssh(h, ". /etc/os-release; echo \"$PRETTY_NAME|$(python3 -V 2>&1|cut -d' ' -f2)"
                   "|$(uname -m)|$(df -h / | awk 'NR==2{print $4}')"
                   "|$(free -m | awk '/Mem:/{printf \"%.1fG\", $2/1024}')\"", timeout=60)
        if r.returncode != 0:
            return record(h, "reachable", False, r.stderr.strip()[:120])
        os_, py, arch, disk, ram = (r.stdout.strip().split("|") + [""] * 5)[:5]
        return record(h, f"{os_}  py{py}  {arch}  free={disk}  ram={ram}", True)

    out = all(fan(hosts, one))
    flush()
    return out


def stage_build() -> Path | None:
    print("\n[build] wheel from the working tree")
    dist = REPO / "dist"
    r = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-q",
                        str(REPO), "-w", str(dist)], capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        record("local", "build wheel", False, r.stderr.strip()[-200:])
        flush()
        return None
    wheels = sorted(dist.glob("kenzy-*.whl"), key=lambda p: p.stat().st_mtime)
    if not wheels:
        record("local", "build wheel", False, "no wheel produced")
        flush()
        return None
    record("local", f"built {wheels[-1].name}", True)
    flush()
    return wheels[-1]


def _server_token() -> str:
    """The token vm1 generated for itself.

    Nodes must present the same one to join, so the server is installed first
    and its token handed to everything else. install.sh generates a token by
    default for server/all profiles.
    """
    r = ssh(SERVER, f"~/{VENV}/bin/python -c \""
                    f"import yaml,pathlib;"
                    f"print(yaml.safe_load(pathlib.Path.home().joinpath('{KHOME}/configs/server.yaml')"
                    f".read_text()).get('discovery',{{}}).get('token','') or '')\"", timeout=60)
    return r.stdout.strip() if r.returncode == 0 else ""


def _install_one(h: str, wheel: Path, token: str) -> bool:
    host = HOSTS[h]
    if not scp(h, wheel, f"/tmp/{wheel.name}") or not scp(h, INSTALLER, "/tmp/install.sh"):
        return record(h, "push wheel + installer", False)

    # No --no-service: the systemd path is precisely what this tier exists to
    # exercise. --yes also accepts the enable-linger prompt, without which the
    # units would die with the SSH session.
    cmd = (f"chmod +x /tmp/install.sh && /tmp/install.sh --profile {host.profile} "
           f"--package /tmp/{wheel.name} --yes")
    # Only net-1 NODES need vm1's token — that's what a join is proven with.
    # The all-in-one hosts run their own server and must keep the token
    # install.sh generates for them; sharing vm1's would let a stray discovery
    # cross-join succeed, which is the collision the pinning exists to prevent.
    if token and host.net == 1 and host.profile == "node":
        cmd += f" --token {shlex.quote(token)}"
    r = ssh(h, cmd)
    if r.returncode != 0:
        tail = (r.stdout + r.stderr).strip().splitlines()[-6:]
        return record(h, "install.sh", False, "; ".join(tail)[:300])
    record(h, "install.sh", True)

    if host.pin_localhost:
        # An all-in-one box already has its server locally; pointing at it
        # directly keeps a neighbour's broadcast from claiming the node.
        #
        # The SCHEME has to be derived, not assumed. install.sh turns TLS on by
        # default for server/all, and a node given a plain ws:// URL against a
        # wss:// server just retries forever ("did not receive a valid HTTP
        # response"). Discovery doesn't have this problem because the mDNS
        # advert carries the scheme — which is exactly why pinning has to
        # reproduce what discovery would have said.
        r = ssh(h, f"~/{VENV}/bin/python -c \""
                   f"import yaml,pathlib;"
                   f"h=pathlib.Path.home();"
                   f"t=(yaml.safe_load((h/'{KHOME}/configs/server.yaml').read_text()) or {{}})"
                   f".get('tls') or {{}};"
                   f"s='wss' if t.get('cert') and t.get('key') else 'ws';"
                   f"p=h/'{KHOME}/configs/node.yaml';"
                   f"d=yaml.safe_load(p.read_text()) or {{}};"
                   f"d['server_url']=s+'://127.0.0.1:8765';"
                   f"p.write_text(yaml.safe_dump(d));"
                   f"print(d['server_url'])\"", timeout=60)
        if not record(h, f"pin server_url -> {r.stdout.strip() or '?'}",
                      r.returncode == 0, r.stderr.strip()[:200]):
            return False
        ssh(h, "systemctl --user restart kenzy-node 2>/dev/null", timeout=120)
    return True


def stage_install(hosts: list[str], wheel: Path) -> bool:
    print("\n[install] running the real installer over SSH")
    ok = True

    # The server first: it is the config authority, and nodes block on their
    # first config frame before initialising audio.
    if SERVER in hosts:
        ok &= _install_one(SERVER, wheel, "")
        if not ok:
            flush()
            print("  server install failed — skipping dependent nodes")
            return False

    token = _server_token() if SERVER in hosts else ""
    if SERVER in hosts:
        record(SERVER, "discovery token " + ("read" if token else "ABSENT"), bool(token))

    rest = [h for h in hosts if h != SERVER]
    if rest:
        results = fan(rest, lambda h: _install_one(h, wheel, token))
        for h, r in zip(rest, results):
            if not r and not HOSTS[h].canary:
                ok = False
    flush()
    return ok


def stage_smoke(hosts: list[str]) -> bool:
    print("\n[smoke] per-host capability checks")

    def one(h: str) -> bool:
        host = HOSTS[h]
        if not scp(h, SMOKE, "/tmp/ci_smoke.py"):
            return record(h, "push smoke script", False)
        profile = "all" if host.profile == "all" else host.profile
        cmd = (f"PATH=$HOME/{VENV}/bin:$PATH ~/{VENV}/bin/python /tmp/ci_smoke.py "
               f"--profile {profile} --expect-model {host.expect_model}")
        r = ssh(h, cmd, timeout=1800)
        detail = "; ".join(ln for ln in r.stdout.splitlines() if "FAIL" in ln)
        return record(h, "ci_smoke", r.returncode == 0, detail[:300])

    results = fan(hosts, one)
    flush()
    return all(r or HOSTS[h].canary for h, r in zip(hosts, results))


def _roster(h: str) -> dict | None:
    """A host's node roster — the nodes that EXIST, not those connected now."""
    r = ssh(h, f"cat ~/{KHOME}/data/nodes.json 2>/dev/null || echo '{{}}'", timeout=60)
    if r.returncode != 0:
        record(h, "read roster", False, r.stderr.strip()[:200])
        return None
    try:
        roster = json.loads(r.stdout or "{}")
    except json.JSONDecodeError as exc:
        record(h, "parse roster", False, str(exc))
        return None
    entries = roster.get("nodes", roster) if isinstance(roster, dict) else {}
    if not isinstance(entries, dict):
        record(h, "roster shape", False, f"expected a dict, got {type(entries).__name__}")
        return None
    return entries


def stage_fleet(hosts: list[str]) -> bool:
    """Did the nodes actually find their server and join it?

    Checked on the net-1 server AND on each all-in-one host, because an
    all-in-one still has to join something. That second check exists because it
    was missing: ci_smoke passed on both solo hosts while their nodes retried
    forever against a ws:// URL their own wss:// server would never answer.
    Import checks cannot see a join — only the roster can.
    """
    print("\n[fleet] rosters")
    ok = True

    for h in [x for x in hosts if HOSTS[x].pin_localhost]:
        entries = _roster(h)
        if entries is None:
            ok = False if not HOSTS[h].canary else ok
            continue
        joined = len(entries) >= 1
        if not record(h, f"own node joined its own server ({len(entries)})", joined) \
           and not HOSTS[h].canary:
            ok = False

    if SERVER not in hosts:
        flush()
        return ok
    r = ssh(SERVER, f"cat ~/{KHOME}/data/nodes.json 2>/dev/null || echo '{{}}'", timeout=60)
    if r.returncode != 0:
        record(SERVER, "read roster", False, r.stderr.strip()[:200])
        flush()
        return False
    try:
        roster = json.loads(r.stdout or "{}")
    except json.JSONDecodeError as exc:
        record(SERVER, "parse roster", False, str(exc))
        flush()
        return False

    # Shape (observed 2026-08-09): {node_id: {ip, last_seen, node_id, room, version}}
    entries = roster.get("nodes", roster) if isinstance(roster, dict) else {}
    if not isinstance(entries, dict):
        record(SERVER, "roster shape", False, f"expected a dict, got {type(entries).__name__}")
        flush()
        return False

    rooms = {e.get("room"): e.get("version") for e in entries.values() if isinstance(e, dict)}
    for n in NET1_NODES:
        # room defaults to the hostname until the dashboard renames it
        ok &= record(SERVER, f"{n} joined", n in rooms,
                     f"roster rooms: {sorted(r for r in rooms if r)}")
    versions = {v for v in rooms.values() if v}
    ok &= record(SERVER, f"all nodes report one version {sorted(versions)}", len(versions) <= 1)
    flush()
    return ok


# --- driver -----------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["check", "build", "install", "smoke", "fleet", "all"])
    ap.add_argument("--hosts", help="comma-separated subset (default: all)")
    args = ap.parse_args()

    hosts = [h.strip() for h in args.hosts.split(",")] if args.hosts else list(HOSTS)
    unknown = [h for h in hosts if h not in HOSTS]
    if unknown:
        print(f"unknown host(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    if not INSTALLER.is_file() and args.stage in ("install", "all"):
        print(f"installer not found at {INSTALLER}", file=sys.stderr)
        return 2

    ok = True
    if args.stage in ("check", "all"):
        ok &= stage_check(hosts)
    if args.stage in ("build", "install", "all"):
        wheel = stage_build()
        if wheel is None:
            return 1
        if args.stage in ("install", "all"):
            ok &= stage_install(hosts, wheel)
    if args.stage in ("smoke", "all"):
        ok &= stage_smoke(hosts)
    if args.stage in ("fleet", "all"):
        ok &= stage_fleet(hosts)

    failed = [r for r in _results if not r.ok and not HOSTS.get(r.host, Host("", "", 0)).canary]
    soft = [r for r in _results if not r.ok and HOSTS.get(r.host, Host("", "", 0)).canary]
    print(f"\n{len(_results) - len(failed) - len(soft)}/{len(_results)} checks passed")
    if soft:
        print("canary (non-fatal): " + ", ".join(f"{r.host}/{r.stage}" for r in soft))
    if failed:
        print("failed: " + ", ".join(f"{r.host}/{r.stage}" for r in failed))
    return 0 if ok and not failed else 1


if __name__ == "__main__":
    sys.exit(main())
