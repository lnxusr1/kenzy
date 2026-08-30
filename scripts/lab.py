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
    scripts/lab.py reset --hosts pi-a    # uninstall+purge: a revert for real hardware
    scripts/lab.py voice                 # spoken end-to-end battery (makes noise)

Rolling back to the pre-install snapshots is deliberately NOT automated here:
it needs credentials on the hypervisor, which this script has no business
holding. Roll back from the Proxmox side, then re-run `all`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "install.sh"          # moved in-repo so CI can reach it too
SMOKE = REPO / "scripts" / "ci_smoke.py"
PROBE = REPO / "voice_probe.py"


def _repo_version() -> str:
    import tomllib
    return str(tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"])

VENV = ".local/share/kenzy/venv"       # install.sh's default
KHOME = ".config/kenzy"                # kenzy_home() default
SSH = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
SSH_RSYNC = ["-e", "ssh -o BatchMode=yes -o ConnectTimeout=8"]
DASH_AUTH = os.environ.get("PROBE_AUTH", "admin:password")


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
    audio: bool = False                 # has the node's speakerphone — the ROOM under test
    talker: bool = False                # has the rig speaker — where the probe PLAYS from
    pin_localhost: bool = False         # all-in-one: point the node at its own server
    canary: bool = False                # failure is reported, never fatal


HOSTS: dict[str, Host] = {
    # Net 1 — the fleet. Discovery is left ON here on purpose: mDNS is a real
    # code path and the one behind the 47-hour orphaned-node incident.
    "vm1": Host("vm1.lan", "server", 1),
    "vm2": Host("vm2.lan", "node", 1, expect_model="tflite"),
    "vm3": Host("vm3.lan", "node", 1, expect_model="tflite"),
    "pi-a": Host("pi-a.lan", "node", 1, expect_model="onnx", audio=True),
    # 2026-08-29: the Y02 rig speaker moved to the workstation and pi-b now
    # holds the second A05U (the co-audible matched pair). The voice stage
    # detects the rig device locally and plays from THIS machine when it's
    # here — this talker flag is the fallback for the rig-on-a-board layout.
    # The original constraint still binds either way: two USB audio devices
    # on one Pi exhaust full-speed isochronous bandwidth ("Not enough
    # bandwidth for altsetting 1") — one audio device per board.
    "pi-b": Host("pi-b.lan", "node", 1, expect_model="onnx", talker=True),
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


def stage_voice(hosts: list[str], cases: list[str] | None) -> bool:
    """The spoken battery, against the lab fleet.

    The ONLY tier involving a microphone, a real wake word, and the whole
    pipeline end to end. Everything else here stops at the package. It has
    found things no other tier could — the one-breath command bug, a name the
    transcriber spells differently per speaker, a cue-grace regression that
    only appeared with real timing.

    Synthesis and playback are split across machines on purpose. Both the
    talker and the node's speakerphone hang off the board, which keeps the rig
    portable — when that board moves to a room no other node can hear, the
    whole rig moves with it and the battery becomes schedulable. But Kokoro has
    no build for the board's Python, and even where it does, Pi-class silicon
    is far too slow to synthesise at run time. So clips are rendered here and
    copied over; `synth()` returns a cached path before it ever imports Kokoro,
    so the speaking host needs no TTS stack at all.

    Deliberately not part of `all`: it speaks aloud, takes minutes, and needs a
    human to have isolated any other node within earshot first.
    """
    listeners = [h for h in hosts if HOSTS[h].audio]
    talkers = [h for h in hosts if HOSTS[h].talker]
    # The rig speaker follows the operator: when the probe's device
    # (PROBE_RIG, default "BY Y02") is plugged into THIS machine, the battery
    # plays from here and needs no remote talker — voice_probe is
    # local-native (the 2026-08-29 conversation passes ran exactly this way).
    # The remote-talker path remains for the rig-on-a-board layout.
    rig_sub = os.environ.get("PROBE_RIG", "BY Y02")
    la = subprocess.run(["aplay", "-l"], capture_output=True, text=True)
    local_talker = la.returncode == 0 and rig_sub.lower() in la.stdout.lower()
    if not listeners or (not talkers and not local_talker):
        record("local", "voice: need a talker and a listener", False,
               f"audio={listeners or 'none'} talker={talkers or 'none'} "
               f"(and no local {rig_sub!r})")
        flush()
        return False
    listener = listeners[0]
    h = "local" if local_talker else talkers[0]
    if not PROBE.is_file():
        record("local", "voice_probe.py present", False, f"not found at {PROBE}")
        flush()
        return False

    print(f"\n[voice] spoken battery — synthesised here, spoken on "
          f"{'THIS machine' if h == 'local' else h}, expecting {listener} to answer")

    # PREFLIGHT. A tier configured for a cloud provider with a placeholder key
    # answers every single case with the error cue — and nothing downstream can
    # see it: the session record shows the text Kenzy COMPUTED, service health
    # reports "ok" because the process is reachable, and the probe passes on a
    # reply that was never spoken. That combination burned a full battery run on
    # 2026-08-09; the whole lab had scaffolded `sb-XXXX` keys from kenzy-init.
    # Cheap to detect, and far better said before making twenty minutes of noise.
    bad = ssh(SERVER, "~/" + VENV + "/bin/python - <<'PY'\n"
              "import pathlib, re\n"
              "env = pathlib.Path.home()/'" + KHOME + "/.env'\n"
              "txt = env.read_text() if env.is_file() else ''\n"
              "keys = dict(re.findall(r'^([A-Z_]+)=(.*)$', txt, re.M))\n"
              # EMPTY is fine — an unset key means that tier is deliberately off,\n"
              # which is documented behaviour. A NON-EMPTY placeholder is the trap:\n"
              # it looks configured, passes every health check, and fails at the\n"
              # moment of use with an auth error nobody downstream can see.\n"
              "def fake(v):\n"
              "    v = v.strip().strip('\"').strip(chr(39))\n"
              "    return bool(v) and ('XXX' in v or v.lower() in ('changeme', 'your-key-here'))\n"
              "bad = [k for k, v in keys.items() if k.endswith('_API_KEY') and fake(v)]\n"
              "print(','.join(bad))\n"
              "PY", timeout=120).stdout.strip()
    if bad:
        record(SERVER, "no scaffolded placeholder credentials", False,
               f"{bad} is a placeholder — that tier answers every case with the error cue")
        print("        Fix the keys, or point those tiers at local providers "
              "(stt=whisper, tts=kokoro are local).")
        flush()
        return False
    record(SERVER, "no scaffolded placeholder credentials", True)
    print("        This SPEAKS ALOUD and actuates real devices via Home Assistant.")

    r = subprocess.run([sys.executable, str(PROBE), "--warm-cache"],
                       cwd=REPO, capture_output=True, text=True, timeout=3600)
    if not record("local", "warm WAV cache", r.returncode == 0,
                  (r.stdout + r.stderr).strip().splitlines()[-1:][0] if r.returncode else ""):
        flush()
        return False

    if h != "local":
        # The probe's own runtime deps. Both ship in the server / stt / tts /
        # llm extras and NOT in `node`, so a board never has them: httpx to
        # poll the dashboard, python-dotenv because the probe loads the same
        # .env the services do. (numpy comes with the node extra; audio goes
        # out through `aplay`, which is why no Python sound library is needed.)
        r = ssh(h, f"~/{VENV}/bin/python -c 'import httpx, dotenv' 2>/dev/null "
                   f"|| ~/{VENV}/bin/pip install -q httpx python-dotenv", timeout=900)
        if not record(h, "probe dependencies", r.returncode == 0, r.stderr.strip()[:200]):
            flush()
            return False

        ok = scp(h, PROBE, "/tmp/voice_probe.py")
        cache = Path.home() / ".cache" / "kenzy-voice-probe"
        rs = subprocess.run(["rsync", "-az", *SSH_RSYNC, f"{cache}/",
                             f"{HOSTS[h].fqdn}:.cache/kenzy-voice-probe/"],
                            capture_output=True, text=True, timeout=1800)
        if not record(h, "push probe + WAV cache", ok and rs.returncode == 0,
                      rs.stderr.strip()[:200]):
            flush()
            return False
    flush()

    # RESULT_TIMEOUT is raised from the default: the lab server is one VM on a
    # mini-PC running STT, the LLM, TTS and speaker ID together, so a patience
    # tuned for prod reads as a failure here.
    # The ROOM is server-owned and set from the dashboard — it is NOT the SSH
    # host name. Passing "pi-a" made presence lookups miss and left Kenzy
    # answering "I don't have a device mapped to pi-a", because room-scoped
    # device resolution had no such room. Ask the server what the room is
    # called, keyed by the node's own id.
    nid = _host_node_id(listener)
    room = listener
    state = _server_state()
    if state and nid:
        for n in state.get("nodes") or []:
            if n.get("node_id") == nid and n.get("room"):
                room = str(n["room"])
                break
    record(listener, f"room resolved to {room!r}", room != listener or not nid,
           "fell back to the host name — is the node registered?")

    # The probe verifies HA entity state itself, so it needs its own read
    # access — without it every device assertion fails as
    # "Illegal header value b'Bearer '", which reads like a broken device and
    # is really an empty key. Passed per invocation rather than written to the
    # board: this is a test rig that gets wiped and reverted, and a credential
    # at rest there outlives the run that needed it.
    ha_key = os.environ.get("HA_API_KEY", "")
    if not ha_key:
        record("local", "HA_API_KEY available for state checks", False,
               "device assertions will fail on an empty Bearer token; "
               "source your .env before running")
    env = (f"PROBE_DASHBOARD=https://{HOSTS[SERVER].fqdn}:8770 "
           f"PROBE_ROOM={room} PROBE_RESULT_TIMEOUT=90 PROBE_SETTLE=6 "
           f"HA_API_KEY={shlex.quote(ha_key)}")
    sel = " --case " + " ".join(cases) if cases else ""
    # Streamed, not captured: a full battery runs for minutes and the per-case
    # lines are the point of watching it.
    if h == "local":
        local_env = dict(os.environ)
        for kv in env.split():
            k, _, v = kv.partition("=")
            local_env[k] = v.strip("'\"")
        run = subprocess.run([sys.executable, str(PROBE), *(
            ["--case", *cases] if cases else [])], cwd=REPO, env=local_env, timeout=5400)
    else:
        run = subprocess.run(["ssh", *SSH, HOSTS[h].fqdn,
                              f"cd ~ && {env} ~/{VENV}/bin/python /tmp/voice_probe.py{sel}"],
                             timeout=5400)
    out = record(h, "voice battery", run.returncode == 0, f"probe exited {run.returncode}")
    flush()
    return out


def stage_reset(hosts: list[str]) -> bool:
    """Return hosts to their pre-install state — a snapshot revert for machines
    that cannot be snapshotted.

    The boards are physical, so a VM-only rollback leaves them carrying the old
    `discovery.token` while the rebuilt server generates a new one. install.sh
    cannot fix that on a re-run: kenzy-init deliberately refuses to clobber an
    existing config, which is right for a real host and wrong for a test rig.

    Uses the product's own uninstaller, so this exercises `--uninstall --purge`
    as a side effect — a path nothing else tests. --purge is the part that
    matters: it removes the config home, and therefore the stale token.
    """
    print("\n[reset] uninstall + purge")

    def one(h: str) -> bool:
        if not scp(h, INSTALLER, "/tmp/install.sh"):
            return record(h, "push installer", False)
        r = ssh(h, "chmod +x /tmp/install.sh && /tmp/install.sh --uninstall --purge --yes",
                timeout=600)
        if r.returncode != 0:
            tail = (r.stdout + r.stderr).strip().splitlines()[-4:]
            return record(h, "uninstall --purge", False, "; ".join(tail)[:300])
        left = ssh(h, "ls -d ~/.config/kenzy ~/.local/share/kenzy 2>/dev/null | tr '\\n' ' '",
                   timeout=60).stdout.strip()
        return record(h, "uninstall --purge", not left, f"still present: {left}")

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

    # Assert the new code LANDED, not that pip exited 0: a rebuilt wheel
    # carrying an unchanged version number used to "install" as
    # already-satisfied and leave stale code serving (2026-08-29: the new
    # kenzy-s2s entry point never appeared — 203/EXEC crash loop). Content is
    # the marker, never the version string — same-version is the exact
    # failing case this exists to catch.
    want = hashlib.sha256(
        zipfile.ZipFile(wheel).read("kenzy/server/server.py")
    ).hexdigest()
    got = ssh(h, f"sha256sum ~/{VENV}/lib/python3*/site-packages/kenzy/server/server.py"
                 " 2>/dev/null | awk '{print $1}'", timeout=60).stdout.strip()
    if got != want:
        detail = f"sentinel hash mismatch (installed {got[:12] or '?'}… != wheel {want[:12]}…)"
        return record(h, "new code landed", False, detail)
    record(h, "new code landed", True)

    # install.sh uses `systemctl --user enable --now`, which starts a stopped
    # unit and leaves a RUNNING one alone. That is right for an installer — the
    # documented upgrade paths are the dashboard buttons and `kenzy-deploy
    # upgrade`, both of which restart. But it means re-installing over a live
    # lab host leaves the old process serving the old code, so the run would
    # test the package on disk and the processes from an hour ago.
    units = ssh(h, "systemctl --user list-units --plain --no-legend 'kenzy-*' 2>/dev/null "
                   "| awk '{print $1}' | tr '\\n' ' '", timeout=60).stdout.strip()
    if units:
        r = ssh(h, f"systemctl --user restart {units}", timeout=300)
        record(h, f"restart {len(units.split())} unit(s)", r.returncode == 0,
               r.stderr.strip()[:200])

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

    # Fetch the token whenever a net-1 NODE is being installed, not merely when
    # the server is in this run. Scoping a run to the boards alone (which is
    # exactly what a physical-host reset needs) would otherwise install them
    # with no token against a server that has one, and the join fails silently.
    need_token = any(HOSTS[h].net == 1 and HOSTS[h].profile == "node" for h in hosts)
    token = _server_token() if need_token else ""
    if need_token:
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


def _server_state() -> dict | None:
    """The server's LIVE view of its nodes.

    Separate from the roster file, which records that a node exists and says
    nothing about whether it works. `audio_ok` lives only here — and a node can
    be connected, current, and stone deaf: audio init is deliberately non-fatal
    so a bad device stays fixable from the dashboard. pi-a ran that way from
    install until 2026-08-09 with every check in this harness green.

    stdlib only, to keep this script runnable from any interpreter.
    """
    import base64
    import ssl
    import urllib.error
    import urllib.request

    base = f"https://{HOSTS[SERVER].fqdn}:8770"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # self-signed by the installer, by design
    auth = base64.b64encode(DASH_AUTH.encode()).decode()
    try:
        req = urllib.request.Request(f"{base}/api/login",
                                     headers={"Authorization": f"Basic {auth}"})
        with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
            cookie = (r.headers.get("set-cookie") or "").split(";")[0]
        if not cookie:
            return None
        # A fresh connection per request: these HTTP endpoints ride the WS port
        # and do not keep-alive, so reusing one client gets a closed connection.
        req = urllib.request.Request(f"{base}/api/state", headers={"Cookie": cookie})
        with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
            return dict(json.loads(r.read().decode()))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _host_node_id(h: str) -> str:
    """A lab host's stable node_id, read from its own node.yaml ("" unknown).

    The server keys everything on node_id; the ROOM is server-owned and
    dashboard-renamable — pi-a's room is 'office', and two fleet checks that
    matched by room==hostname called a perfectly healthy node absent. Identity
    is the id, never the name.
    """
    r = ssh(h, f"~/{VENV}/bin/python -c \""
               f"import yaml,pathlib;"
               f"print((yaml.safe_load((pathlib.Path.home()/"
               f"'{KHOME}/configs/node.yaml').read_text()) or {{}}).get('node_id',''))\"",
            timeout=60)
    return r.stdout.strip() if r.returncode == 0 else ""


def audio_failures(state: dict, hosts: list[str]) -> list[str]:
    """Hosts that carry a microphone but report it not working. Matched by
    node_id (room falls back only when the id is unreadable)."""
    nodes = state.get("nodes") or []
    by_id = {n.get("node_id"): n for n in nodes}
    by_room = {n.get("room"): n for n in nodes}
    bad = []
    for h in hosts:
        if not HOSTS[h].audio:
            continue
        nid = _host_node_id(h)
        n = by_id.get(nid) if nid else None
        if n is None:
            n = by_room.get(h)
        if n is None or not n.get("audio_ok"):
            why = (n or {}).get("audio_error") or "not reported by the server"
            bad.append(f"{h}: {why}")
    return bad


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
        # Identity is the node_id (rooms are dashboard-renamable — pi-a is
        # 'office'); the room==hostname match survives only as a fallback for
        # a host whose config can't be read.
        nid = _host_node_id(n)
        joined = nid in entries if nid else n in rooms
        ok &= record(SERVER, f"{n} joined", joined,
                     f"roster rooms: {sorted(r for r in rooms if r)}")
    # Agreement is not correctness: every node reporting the same STALE version
    # passed this check while the freshly-installed package sat unused on disk.
    # Assert against the version actually being built.
    # A node with a microphone must actually have it open. Joining proves the
    # socket works, not that the room can hear.
    if any(HOSTS[x].audio for x in hosts):
        state = _server_state()
        if state is None:
            ok &= record(SERVER, "read live node state", False, "dashboard unreachable")
        else:
            bad = audio_failures(state, hosts)
            ok &= record(SERVER, "audio hosts report audio_ok", not bad, "; ".join(bad)[:300])

    want = _repo_version()
    versions = {v for v in rooms.values() if v}
    ok &= record(SERVER, f"all nodes run the built version ({want})",
                 versions == {want}, f"roster reports {sorted(versions)}")
    flush()
    return ok


# --- driver -----------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage",
                    choices=["check", "reset", "build", "install", "smoke", "fleet",
                             "voice", "all"])
    ap.add_argument("--cases", nargs="*",
                    help="voice stage: run only these battery cases (see voice_probe --list)")
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
    if args.stage == "reset":
        return 0 if stage_reset(hosts) else 1
    if args.stage == "voice":
        # Deliberately NOT part of `all`: it speaks aloud, takes minutes, and
        # needs a human to have isolated any other node in the room.
        return 0 if stage_voice(hosts, args.cases) else 1
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
