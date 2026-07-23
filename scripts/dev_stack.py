#!/usr/bin/env python3
"""Run the whole Kenzy stack as local processes for quick testing.

One terminal, one Ctrl-C. Starts the server first (it's the config authority),
then the backend services, which config-pull from it (KENZY_SERVER_URL is set
for them so nothing waits on mDNS). Each service's output is line-prefixed and
colorized so five interleaved logs stay readable.

    python dev_stack.py                    # server + stt + tts + llm + speaker
    python dev_stack.py --only server,llm  # a subset
    python dev_stack.py --skip speaker     # all but one
    python dev_stack.py --node             # also start a room node (needs audio)
    python dev_stack.py --local            # pin services to local/packaged config
                                           #   (ignores the central store)

By default the backend services **config-pull from the server** — the real boot
path, which is what makes the dashboard work: Services-tab edits write the
central configs/services/ overrides, and a pulled service reads them on
restart. (A local-pinned default was tried and reverted: it silently turned
dashboard config edits into no-ops.) Use --local only when the central store
carries tuning this machine can't satisfy — and prefer fixing the store.

Ctrl-C once: SIGINT reaches every child via the foreground process group; each
service shuts down cleanly and the script reaps them (SIGTERM→SIGKILL after a
grace period for anything wedged). A service that dies on its own is reported
loudly but the rest of the stack stays up — handy while iterating on one piece.

Dev tool only — not packaged. For real deployments use systemd units
(install.sh / kenzy-deploy).
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time

SERVICES = ["server", "stt", "tts", "llm", "speaker"]  # start order; node is opt-in


def _server_scheme() -> str:
    """ws, or wss when the dev server config enables TLS."""
    try:
        import yaml

        with open(os.path.join("configs", "server.yaml")) as f:
            cfg = yaml.safe_load(f) or {}
        tls = cfg.get("tls") or {}
        if tls.get("cert") and tls.get("key"):
            return "wss"
    except Exception:
        pass
    return "ws"

_COLORS = ["\033[36m", "\033[32m", "\033[33m", "\033[35m", "\033[34m", "\033[91m"]
_RESET = "\033[0m"
_BOLD = "\033[1m"

_GRACE_S = 10.0  # after Ctrl-C: wait this long before SIGTERM, then 4s more to SIGKILL


def _pump(name: str, color: str, proc: subprocess.Popen[str], use_color: bool) -> None:
    """Copy a child's output to ours, one prefixed line at a time."""
    prefix = f"{color}[{name:<7}]{_RESET} " if use_color else f"[{name:<7}] "
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(prefix + line)
        sys.stdout.flush()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", help="comma-separated subset (e.g. server,llm)")
    ap.add_argument("--skip", help="comma-separated services to leave out")
    ap.add_argument("--node", action="store_true", help="also start kenzy-node")
    ap.add_argument(
        "--local",
        action="store_true",
        help="pin services to local/packaged configs (dashboard service edits won't apply!)",
    )
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()

    names = list(SERVICES)
    if args.only:
        chosen = [s.strip() for s in args.only.split(",") if s.strip()]
        unknown = [s for s in chosen if s not in SERVICES + ["node"]]
        if unknown:
            ap.error(f"unknown service(s): {', '.join(unknown)}")
        names = [s for s in SERVICES if s in chosen]
        if "node" in chosen:
            args.node = True
    if args.skip:
        skipped = {s.strip() for s in args.skip.split(",")}
        names = [s for s in names if s not in skipped]
    if args.node:
        names.append("node")
    if not names:
        ap.error("nothing to start")

    use_color = sys.stdout.isatty() and not args.no_color

    env = os.environ.copy()
    # Backend services config-pull from the server; point them straight at the
    # local one so nothing waits on an mDNS browse. (Respects an existing value.)
    # When configs/server.yaml carries a tls: block the server speaks wss —
    # serviceboot maps that to https for the pull, unverified by default
    # (the dev pair is self-signed; KENZY_TLS_VERIFY stays unset).
    env.setdefault("KENZY_SERVER_URL", f"{_server_scheme()}://127.0.0.1:8765")

    procs: dict[str, subprocess.Popen[str]] = {}
    threads: list[threading.Thread] = []

    def start(name: str, color: str) -> None:
        cmd = [f"kenzy-{name}"]
        # --local: an explicit path forces local load (configs/<svc>.yaml if
        # present, else the pinned packaged default) instead of the real
        # config-pull boot path. Off by default — pulling is what makes the
        # dashboard's Services editor actually apply to a dev stack.
        if name in ("stt", "tts", "llm", "speaker") and args.local:
            local = os.path.join("configs", f"{name}.yaml")
            if not os.path.exists(local):
                # Pin the packaged default explicitly — otherwise the resolver
                # walks on to ~/.config/kenzy, and this machine's personal config
                # (possibly tuned for different hardware) hijacks the dev stack.
                from kenzy.config import packaged_config

                local = str(packaged_config(name))
            cmd.append(local)
        procs[name] = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,  # the server has an interactive stdin loop —
            stdout=subprocess.PIPE,  # don't let children fight over the terminal
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        t = threading.Thread(target=_pump, args=(name, color, procs[name], use_color), daemon=True)
        t.start()
        threads.append(t)

    interrupted = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: interrupted.set())
    signal.signal(signal.SIGTERM, lambda *_: interrupted.set())

    bold = _BOLD if use_color else ""
    reset = _RESET if use_color else ""
    print(f"{bold}dev-stack: starting {', '.join(names)} — Ctrl-C stops everything{reset}")

    try:
        for i, name in enumerate(names):
            start(name, _COLORS[i % len(_COLORS)])
            if name == "server" and len(names) > 1:
                time.sleep(1.0)  # give the config authority a head start

        # Supervise: report any child that dies; run until Ctrl-C.
        reported: set[str] = set()
        while not interrupted.is_set():
            for name, p in procs.items():
                rc = p.poll()
                if rc is not None and name not in reported:
                    reported.add(name)
                    print(
                        f"{bold}dev-stack: kenzy-{name} EXITED rc={rc} "
                        f"(rest of the stack still up){reset}"
                    )
            if all(p.poll() is not None for p in procs.values()):
                print(f"{bold}dev-stack: all services exited{reset}")
                return 1
            interrupted.wait(0.5)
    finally:
        # Ctrl-C already delivered SIGINT to the children (shared foreground
        # process group) — reap gracefully, then escalate for anything wedged.
        deadline = time.monotonic() + _GRACE_S
        for name, p in procs.items():
            while p.poll() is None and time.monotonic() < deadline:
                time.sleep(0.1)
            if p.poll() is None:
                p.terminate()
        for name, p in procs.items():
            try:
                p.wait(timeout=4.0)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        codes = ", ".join(f"{n}={p.returncode}" for n, p in procs.items())
        print(f"\n{bold}dev-stack: stopped ({codes}){reset}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
