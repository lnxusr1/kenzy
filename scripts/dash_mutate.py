"""Fire dashboard WS mutations from a script — the lab's remote control.

The dashboard's mutation channel (trigger, stop, force_wake, set_ignore_audio,
set_muted, …) is how live tests script who-hears-what without touching
hardware: force one room deaf, force-wake another, flip mid-answer. This
client does the auth dance (Basic login → cookie → WS) and sends each JSON
mutation given on the command line, printing the ack.

    python scripts/dash_mutate.py '{"type":"set_ignore_audio","node":"<id>","ignore":true}'
    python scripts/dash_mutate.py '{"type":"force_wake","node":"<id>"}'
    python scripts/dash_mutate.py '{"type":"set_ignore_audio","node":"<id>","ignore":false}' \
                                  '{"type":"force_wake","node":"<other>"}'

Multiple mutations run in order over one connection — useful for atomic-ish
flips ("B deaf + A live + force-wake A" lands within ~100 ms).

Environment (defaults are the dev/lab dashboard):
    KENZY_DASH_URL   default https://127.0.0.1:8770
    KENZY_DASH_USER  default admin
    KENZY_DASH_PASS  default password

Never name a mutation payload key "id" — the WS envelope uses it for
request/ack correlation (this script assigns them).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import ssl
import sys
import urllib.request


def _cookie(base: str, user: str, password: str, ctx: ssl.SSLContext) -> str:
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    resp = opener.open(
        urllib.request.Request(f"{base}/api/login", headers={"Authorization": f"Basic {auth}"}),
        timeout=10,
    )
    return resp.headers.get("Set-Cookie", "").split(";")[0]


async def _run(base: str, cookie: str, ctx: ssl.SSLContext, mutations: list[dict]) -> int:
    import websockets  # deferred: not in the node extra

    ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
    failures = 0
    async with websockets.connect(ws_url, ssl=ctx, additional_headers={"Cookie": cookie}) as ws:
        await ws.recv()  # state snapshot
        for i, mutation in enumerate(mutations):
            mutation["id"] = f"m{i}"
            await ws.send(json.dumps(mutation))
            while True:
                reply = json.loads(await ws.recv())
                if reply.get("id") == f"m{i}":
                    ok = bool(reply.get("ok"))
                    failures += 0 if ok else 1
                    print(
                        f"{mutation['type']} {mutation.get('node', '')[:8]} -> "
                        f"{'ok' if ok else 'FAILED'} {reply.get('error') or ''}".rstrip()
                    )
                    break
    return failures


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    try:
        mutations = [json.loads(a) for a in sys.argv[1:]]
    except json.JSONDecodeError as exc:
        print(f"not JSON: {exc}", file=sys.stderr)
        return 2
    base = os.environ.get("KENZY_DASH_URL", "https://127.0.0.1:8770").rstrip("/")
    user = os.environ.get("KENZY_DASH_USER", "admin")
    password = os.environ.get("KENZY_DASH_PASS", "password")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # lab dashboards are self-signed
    cookie = _cookie(base, user, password, ctx)
    return asyncio.run(_run(base, cookie, ctx, mutations))


if __name__ == "__main__":
    raise SystemExit(main())
