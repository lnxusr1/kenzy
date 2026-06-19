# Dashboard

`kenzy-server` can serve an **opt-in** web dashboard — a fleet manager for your
Kenzy deployment. It is **off by default** and adds zero overhead when disabled
(nothing is mounted, no node-side cost). When enabled it gives you one place to see
every room node and backend service, configure nodes, control them, send
announcements, and read logs.

!!! warning "Keep it on the LAN"
    Login runs over **plaintext HTTP** and defaults to `admin` / `password`. Bind the
    dashboard to localhost or your LAN, change the password (below), and **never
    port-forward** the dashboard port to the public internet.

## Enabling it

In `configs/server.yaml`:

```yaml
dashboard:
  enabled: true
  bind: "127.0.0.1"     # or a LAN address; never the public internet
  port: 8770
  controls: true        # allow edits/actions (false = read-only)
  logs: true            # enable the log viewer
```

Restart `kenzy-server` and open `http://<bind>:<port>/dashboard` (e.g.
`http://127.0.0.1:8770/dashboard`). See the full key reference in
[Server Configuration](configuration/server.md#dashboard).

## Logging in

The default login is **`admin` / `password`**. Change it on the server host with the
`kenzy-passwd` CLI (it rewrites `dashboard.auth` in `server.yaml`):

```bash
kenzy-passwd            # prompts for username + new password
```

You can also change the password from the dashboard's **Settings** page. A password
change takes effect immediately and signs out other sessions.

## Fleet view

The landing page lists:

- **Room nodes** — one card per connected node, showing its room name, a short node
  id, IP address, and live status (idle / streaming). Cards flagged **⚑ unconfigured**
  have no saved per-node config yet. A **Configure** button opens the node editor.
- **Backend services** — STT, TTS, LLM, and Speaker health (from each service's
  `/health`), with a few details (model, voice, provider).

The status pill (top right) shows whether the live channel is connected and keeps a
running "last update" time. State is pushed live over a WebSocket, falling back to
polling if that drops.

When `controls` is on, an **announce** composer lets you type a message and speak it
aloud on every connected node at once (synthesised once via kenzy-tts, streamed to all
rooms — an intercom).

## Configuring a node

Open a node's **Configure** page to:

- **Rename its room** — the room name is the node's friendly label everywhere and is
  sent to the assistant as context. It is **server-owned**: stored in
  `configs/nodes/<node_id>.yaml`, applied live if the node is connected and otherwise
  pulled on its next connect (so you can name a node before it's ever booted). Identity
  is the stable `node_id`, so renaming a room never orphans its config.
- **Edit per-node settings** — wake-word threshold/VAD, silence/VAD timing, audio
  device, sample rates, wake-word models, and sound files. Saved values are written to
  `configs/nodes/<node_id>.yaml` and **live-re-pushed** to the connected node. Each key
  shows a **live** or **restart** badge: live keys apply immediately on save; hardware
  keys are applied on the node's next boot or via the Restart button.
- **Control the node** — **Trigger** (start a session), **Stop**, or **Restart** (the
  node re-execs itself, with or without systemd).

Secrets (API keys) are never served to a node and never editable here.

## Configuring backend services

The **Services** tab lists the configured backend services (STT, TTS, LLM, Speaker)
with live health. Open one to edit its **effective config** in a generic editor —
each field is the packaged default or your stored override. Saving writes
`configs/services/<service>.yaml` on the server and **restarts the service** so the new
config takes effect (the service re-pulls on boot); a separate **Restart** button
restarts without editing. Secrets (API keys) are read from the service host's
environment and are never shown or stored here. Requires `dashboard.controls: true`.

## Logs

With `dashboard.logs: true`, the **Logs** tab pulls a bounded in-memory buffer from a
source you pick: the server, any backend service, or any connected node. Filter by
level. Logs are pull-based — a node only keeps a buffer when the dashboard asks it to,
so a dashboard-less server adds no node overhead.

## Settings

The **Settings** page shows read-only system info (Kenzy version, server and dashboard
binds, mDNS discovery, configured backends, feature-flag state) and lets you **change
the dashboard password**.

## Permissions & security

- All read views are open to anyone who can reach the bind address; **mutations**
  (config edits, rename, controls, announce) require login and `dashboard.controls`.
- `dashboard.auth_token` is an optional bearer for API/CLI clients; browsers use a
  signed, HttpOnly session cookie from the login form.
- The `discovery.token` (or `KENZY_SERVICE_TOKEN`) doubles as a service-to-service
  bearer the server uses for its backend calls and log proxying.
