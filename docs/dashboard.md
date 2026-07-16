# Dashboard

The dashboard is the web page where you run your Kenzy home: see every room and
service at a glance, name rooms, pick and tune microphones, enroll voices, send
announcements, read logs, and install updates — all from a browser, no terminal
needed after install. It's served by `kenzy-server` and is **on by default**,
reachable from any machine on your network at `http://<server>:8770`. (Set
`dashboard.enabled: false` to turn it off entirely — when disabled, nothing is
mounted and it adds zero overhead.)

!!! warning "Keep it on the LAN"
    Login runs over **plaintext HTTP** by default and defaults to `admin` / `password`.
    Bind the dashboard to localhost or your LAN, change the password (below), and
    **never port-forward** the dashboard port to the public internet. To encrypt it,
    [enable TLS](#https-optional).

## Enabling it

The shipped config already has it on. The relevant block in `configs/server.yaml`:

```yaml
dashboard:
  enabled: true
  bind: "0.0.0.0"       # reachable on your LAN (the default); "127.0.0.1" = this machine only
  port: 8770
  controls: true        # allow edits/actions (false = read-only)
  logs: true            # enable the log viewer + Activity tab
```

Open `http://<server>:8770` in a browser (the same machine can use
`http://localhost:8770`). After changing this block, restart `kenzy-server`. See the
full key reference in [Server Configuration](configuration/server.md#dashboard).

## Logging in

The default login is **`admin` / `password`**. Change it on the server host with the
`kenzy-passwd` CLI (it writes `dashboard.auth` into the `server.local.yaml`
override layer, so the login survives upgrades and deploy config syncs):

```bash
kenzy-passwd            # prompts for username + new password
```

You can also change the password from the dashboard's **Settings** page. A password
change takes effect immediately and signs out other sessions.

## Fleet view

The landing page lists:

- **Room nodes** — one card per connected node, showing its room name, a short node
  id, IP address, installed Kenzy **version**, live status (idle / streaming), and a
  **system** row with the node's CPU, RAM, and disk usage plus its temperature
  (refreshed every ~30 seconds; the temperature turns red at 80°C — Raspberry Pi
  throttling territory, usually a sign the node wants a heatsink or better airflow).
  Cards flagged **⚑ unconfigured** have no saved per-node config yet; rooms with
  `hardware_aec: false` carry a **no AEC** badge. A **Configure** button opens the
  node editor.
- **Backend services** — STT, TTS, LLM, and Speaker health (from each service's
  `/health`), with a few details (version, model, voice, provider).

The status pill (top right) shows whether the live channel is connected and keeps a
running "last update" time. State is pushed live over a WebSocket, falling back to
polling if that drops.

When `controls` is on, an **announce** composer lets you type a message and speak it
aloud on every connected node at once (synthesised once via kenzy-tts, streamed to all
rooms — a one-way public-address broadcast). You can also trigger this by voice
("tell everyone dinner's ready"); for a live *two-way* call between rooms, see the
intercom skill in [Built-in Skills](skills/builtin.md#intercom).

## Configuring a node

Open a node's **Configure** page to:

- **Rename its room** — the room name is the node's friendly label everywhere and is
  sent to the assistant as context. It is **server-owned**: stored in
  `configs/nodes/<node_id>.yaml`, applied live if the node is connected and otherwise
  pulled on its next connect (so you can name a node before it's ever booted). Identity
  is the stable `node_id`, so renaming a room never orphans its config.
- **Set up / calibrate audio** — the **Set up / calibrate audio…** button picks the
  audio device, then launches the guided calibration session (the same one
  "Hey Kenzy, calibrate" runs). See [Calibrating a node's audio](#calibrating-a-nodes-audio)
  below. The raw audio keys (`audio_device`, sample rates) also remain in the settings
  list for direct editing / pre-seeding an offline node.
- **Edit per-node settings** — audio device + sample rates, wake-word threshold/VAD,
  silence/VAD timing, wake-word models, sound files, volume (a slider), and the node's
  `log_level` / `log_capture_level`. Saved
  values are written to `configs/nodes/<node_id>.yaml` and **live-re-pushed** to the
  connected node. Each key shows a **live** or **restart** badge: live keys apply
  immediately on save; hardware keys are applied on the node's next boot or via the
  Restart button. Options with a fixed set of values (log levels, on/off, etc.) are
  dropdown choosers; numeric fields are number inputs.
- **Control the node** — **Trigger** (start a session), **Stop**, **Restart** (the
  node re-execs itself, with or without systemd), or **Upgrade** (the node pip-upgrades
  `kenzy[node]`, honoring `constraints.txt`, and reconnects on the new version — watch the
  version on its fleet card to confirm).

Secrets (API keys) are never served to a node and never editable here.

## Calibrating a node's audio

The right `audio_device`, `silence_rms_threshold`, `wakeword_threshold`,
`wakeword_vad_threshold`, and `hardware_aec` depend on each room's hardware and noise
level, so the defaults are rarely ideal. Calibration is **one guided session** with two
ways in — and it needs a person standing where they'd normally talk either way:

- Say **"Hey Kenzy, calibrate"** in the room: she speaks the instructions.
- Click **Set up / calibrate audio…** on the node's Configure page: the same session
  runs with the instructions shown in the dashboard (the node beeps once instead of
  speaking — no TTS needed). Requires the node connected and `dashboard.controls: true`.

The dashboard flow starts with the one step that needs a human at a screen:

1. **Audio device** — choose the room's mic/speaker from the list the node reported (no
   need to run `kenzy-devices` on the box). Because the device is a hardware key, the
   wizard **saves it, restarts the node, and waits for it to reconnect** before
   continuing. (Click **Keep current** to skip if the device is already right.)
2. **Calibration** — one automated pass, watched live in the dashboard:
   - **Echo check** — the node plays a known signal through its own speaker and
     measures how much leaks back into the mic. This sets `hardware_aec`
     automatically (present ⇒ you can interrupt her mid-sentence; absent ⇒
     half-duplex mode, and intercom/alarms are disabled on that node). An ambiguous
     reading — or a muted/very quiet speaker — changes nothing.
   - **Quiet phase** — a few seconds of room silence (a loud noise restarts it once).
   - **Wake phase** — say "Hey Kenzy" four times from where you'd normally speak. The
     repetitions double as a sample of your voice level, and
     `silence_rms_threshold` is **anchored to your voice** (not the quiet floor), so
     an appliance starting up later stays below it. `wakeword_threshold` and
     `wakeword_vad_threshold` come from the same pass.
   - **Apply + verify** — values apply automatically (the node restarts once if the
     VAD gate changed), then she really listens: say "Hey Kenzy" (then "never mind")
     to prove the wake word works — if she misses it, the threshold is nudged down,
     at most twice.

The result panel shows every applied value, the echo-cancellation verdict, a
**noise-to-speech separation** rating (good / marginal / poor), and whether the wake
word verified live. Anything that couldn't be measured cleanly keeps its previous
value — and the panel says so. You can still fine-tune the numbers directly in the
settings grid afterward.

### Headless calibration (no dashboard)

On a node with no server flow available, run the measurement locally:

```bash
kenzy-node --calibrate
```

It walks through the same two phases (quiet, then speak) with the same voice-anchored
math and prints the suggested thresholds — no echo check, since that needs the
server-driven playback path. Because node config is **server-owned** (pulled on
connect), the values aren't written locally — apply them on the server, either from
the dashboard or by adding them to `configs/nodes/<node_id>.yaml` (this node) or
`node_defaults` in `server.yaml` (all nodes).

## Configuring backend services

The **Services** tab lists the configured backend services (STT, TTS, LLM, Speaker)
with live health. Open one to edit its **effective config** in a generic editor —
each field is the packaged default or your stored override. Saving writes
`configs/services/<service>.yaml` on the server and **restarts the service** so the new
config takes effect (the service re-pulls on boot); a separate **Restart** button
restarts without editing, and an **Upgrade** button pip-upgrades that service to the
latest release (honoring `constraints.txt`) and restarts it — the install runs in the
background and reports the result. Secrets (API keys) are read from the service host's
environment and are never shown or stored here. Requires `dashboard.controls: true`.

## Skills

The **Skills** tab shows everything loaded by `kenzy-llm`, grouped into
**collapsible modules** — one accordion group per skill file (`home assistant`,
`lists`, `schedule`, …), because a "feature" is usually several skills plus
their fast intents. Each group header shows a skill/fast-intent count and a
**Disable all / Enable all** toggle for the whole module; expand a group to see
its member skills, each with a one-line description, an **invocation count**,
and its own individual toggle.

All toggles take effect **immediately, without restarting the service** (a
disabled skill stays loaded but is gated out of the tool list and the fast
path) and **persist** to `configs/services/llm.yaml` (`skills.disabled`, which
accepts function names and module names). Group headers also carry honesty
badges: a module that *depends* on another (lists requires `home_assistant` —
they're stored in HA's to-do entities) is marked **inactive** while its
dependency is off, and modules with wider blast radius say what else they power
(disabling `home_assistant` also idles the Home Assistant screen and lists).
Without `controls`, the tab is read-only.

## Home Assistant

The **Home Assistant** tab edits the device **curation** layer for the Home Assistant
skill — the small set of things HA can't store. The device inventory itself is pulled
**live from HA** (via `kenzy-llm`) and shown as a tree (`floor → area → domain → entity`);
you don't list devices here. Each entity row has:

- **aliases** — extra spoken names ("the lamp", "black light")
- **note** — free-form context handed to the resolver ("the light by the chair")
- **default** — include in this room's bare "turn on the lights" set
- **in groups** — uncheck to keep it addressable by name but out of group commands (a bare "turn off all the lights" still includes it; only **exclude** removes it from voice entirely)
- **exclude** — remove it from voice control entirely

plus **bulk exclusions** (patterns/domains/areas) for things like smart-plug status LEDs
that show up as controllable lights, and a **Lists** section for the shopping/to-do
voice layer — pick which HA to-do list a bare "the list" means and give lists spoken
aliases ("the groceries"); see [Built-in Skills → Lists](skills/builtin.md#lists).
Saving writes `curation.yaml` and refreshes the topology immediately. The tab needs `kenzy-llm` reachable and `dashboard.controls: true`
to edit (read-only otherwise). See [Home Assistant](skills/home-assistant.md).

Two states the tab is honest about: with the `home_assistant` module **disabled**
(Skills tab), a warning banner says nothing here takes effect until it's
re-enabled — but the screen stays editable, so you can stage curation before
flipping the feature on. And with no `HA_API_KEY` configured at all, the tab
shows step-by-step connection guidance instead of an error.

## Speakers

The **Speakers** tab manages the enrolled voice profiles held by `kenzy-speaker`. It
lists each enrolled voice with its **sample count** and the service's current
**identify threshold**. With `dashboard.controls: true` you can **rename** or **delete**
a profile. Deleting is permanent; renaming just relabels the stored embeddings.

The dashboard does **not** record audio in the browser. To add a voice, either run
`kenzy-enroll` on the server host, or use **Enroll from a room**: pick a connected room
node and a name, and Kenzy prompts the person at that room to say a few sentences and
enrolls them through the room's mic (enrolling an existing name adds more samples to it).
Because this is an authenticated, `controls`-gated operator action, it works regardless
of the speaker service's `allow_voice_enroll` setting (which only governs the hands-free
"Hey Kenzy, enroll me as…" voice command). Requires `dashboard.controls: true`.

## Scheduled

The **Scheduled** tab lists the active **timers, alarms, and reminders** held by the
server — kind, name/text, target room, when it fires (live countdown for timers), and
any recurrence — with a per-entry **Cancel** button (requires `dashboard.controls`).
Entries are set by voice (see [Timers, Alarms & Reminders](skills/builtin.md#timers-alarms-reminders))
and persist across server restarts. The view is available to any logged-in user —
deliberately not gated behind `dashboard.logs`, since these are future announcements
the operator needs to see to manage.

## Activity

With `dashboard.logs: true`, the **Activity** tab shows the recent voice interactions
the server has handled, so you can see what Kenzy heard, how it answered, and where the
time went. Each entry shows:

- the **transcript** (what was heard), the identified **speaker** and **room**, and the
  spoken **response**;
- a **fast / LLM** tag — whether the deterministic fast path handled it or it went to the
  language model;
- a **latency breakdown** (capture = STT + speaker ID in parallel, then LLM, then TTS)
  and the total response time.

The header summarises the **fast-path hit rate** and **average response time** across the
recent window. It's a bounded in-memory ring (no disk, ~200 entries) that updates live;
because entries include transcripts it's gated by the same `dashboard.logs` flag as the
log viewer, and nothing is recorded when that's off.

The latency bars share **one time scale** across all shown interactions (the legend notes what full width represents), so segment widths are directly comparable run-to-run — a slow LLM call is visibly wider than a fast one, and fast-path replies show as slivers.

## Logs

With `dashboard.logs: true`, the **Logs** tab pulls a bounded in-memory buffer from a
source you pick: the server, any backend service, or any connected node. Filter by
level (down to **TRACE**). Logs are pull-based — a node only keeps a buffer when the
dashboard asks it to, so a dashboard-less server adds no node overhead.

Each source captures down to its **`log_capture_level`** (default `debug`),
independently of what it prints to its own console (`log_level`). So a node logging
INFO to its console can still surface DEBUG in the viewer. Levels below a source's
capture level aren't kept — raise that source's `log_capture_level` (e.g. to `trace`,
which includes the node's per-frame audio logs) from its config to see deeper.

**Temporary TRACE capture (nodes).** The node's most detailed logs (per-frame
RMS/VAD) are at TRACE, off by default to avoid flooding. When a node is the selected
source, a **Capture TRACE** button (with a duration picker) boosts that node to TRACE
capture live for the chosen window and then auto-reverts — no restart, nothing
persisted. Refresh during/after the window to view the captured detail. Requires
`dashboard.controls`.

## Settings

The **Settings** page shows system info (Kenzy version, server and dashboard binds, mDNS
discovery), an **update check**, a **backup download**, the **node join token**, and lets
you **change the dashboard password** and edit a **scoped subset of the server's own
configuration**.

The **Backup** section downloads a `.tar.gz` of the deployment's state — node/service
settings, voice profiles (fetched from the speaker host when remote), HA curation,
custom skills — restorable with `kenzy-init --restore`. Two opt-in toggles widen the
scope: **include secrets** (`.env` — the archive then carries live API keys) and
**include everything** (`models/`). See [Backup & Restore](backup-restore.md).

The **API keys** section is a **write-only** secret editor: pick a key
(`OPENAI_API_KEY`, `HA_API_KEY`, `HF_TOKEN`, or a custom name), paste a value, and it's
written to the server host's `.env` — values are never displayed or logged; the page
only shows which names are *set*. Restart the affected services (Services tab) to apply.

As of 3.12 this is **fleet-wide**: with TLS enabled, the server hands each backend
service the keys it needs over the authenticated config channel, so a key set here
takes effect on every host — you no longer maintain a separate `.env` per machine.
(Without TLS, secrets aren't served, and remote hosts fall back to their own local
`.env`.) The server's value wins, so rotating a key here actually rotates it everywhere.
Requires `dashboard.controls`. Unless you've [enabled TLS](#https-optional), the
dashboard runs over plain HTTP — enter keys from a machine on your own network.

The **Updates** section compares the installed version against the latest `kenzy` release
on PyPI and flags when one is available. It's checked lazily (only when you open Settings,
cached ~1 hour) and degrades gracefully on an offline/air-gapped host. When an update is
available and `dashboard.controls` is on, an **Upgrade server** button runs
`pip install -U "kenzy[server]"` in the server's venv (honoring your `constraints.txt`
pins, pinned to the target version) and then restarts the server. The install runs in the
background — the dashboard disconnects while it works (a few minutes) and reconnects when
the server is back on the new version; a failed install is reported and leaves the server
running as-is. This upgrades the **server host only**; backend services and room nodes are
upgraded separately.

Under **Node provisioning** the page displays the `discovery.token` (the shared secret a
node presents to join, also the service-to-service bearer) with a copy button, so you can
paste it into a node install — `kenzy-init --profile node --token …` or the installer's
`--token`. This is the one secret the dashboard surfaces, deliberately: it's a
*provisioning* value an admin needs, shown only over the authenticated Settings page (not
an upstream API key). If no token is set, the page warns that any device on the network
can register as a node.

The **Server configuration** editor exposes the safe-to-change keys: the dashboard
sub-flags (`logs`, `controls`), each backend service's `url`/`timeout`, the unknown-speaker
label, and mDNS `discovery.enabled`/`instance`. Saving writes a `server.local.yaml`
override layered over your hand-edited `server.yaml` (so comments are preserved) and
**restarts the server** to apply it — the dashboard briefly disconnects and reconnects.
For safety, lockout/secret-sensitive keys (server host/port, the dashboard bind/port,
the login credentials, and the `discovery.token`) are **not** editable here and stay
file- or CLI-managed. Because this editor is the way to turn `controls` on in the first
place, it requires login but not `controls`.

## Permissions & security

- **Both reads and mutations require login.** All `/api/*` endpoints (fleet state, node
  config, logs, transcripts) need a valid session; only the login/logout/me endpoints and
  the static assets are public. **Mutations** (config edits, rename, controls, announce)
  additionally require `dashboard.controls`.
- `dashboard.auth_token` is an optional bearer for API/CLI clients; browsers use a
  signed, HttpOnly session cookie from the login form.
- Change the default password promptly — the dashboard warns (startup log + a Settings
  banner) while it's still on `admin/password`.
- The `discovery.token` (or `KENZY_SERVICE_TOKEN`) doubles as a service-to-service
  bearer the server uses for its backend calls and log proxying.
- The `/ws` channel (which carries all mutations) rejects **cross-site** handshakes
  (the browser `Origin` must match the `Host`). For extra **DNS-rebinding** protection
  when you serve the dashboard under a fixed name, set `dashboard.allowed_hosts`.

### HTTPS (optional)

Login and traffic are **plaintext by default** — fine on a trusted wired LAN, weaker on
Wi-Fi. Two ways to encrypt it:

- **Built-in TLS** — set `tls: {cert, key}` in `server.yaml` and the dashboard serves
  **https** (and the node WebSocket port **wss**) directly. A self-signed cert works;
  your browser shows a one-time warning (or install the cert). See
  [Server Configuration → TLS](configuration/server.md#tls-optional) for the
  one-line `openssl` command and how Kenzy's own clients handle it.
- **Reverse proxy** terminating TLS (Caddy gets you an automatic cert for a routable
  name; nginx/Traefik work with your own cert). Have the proxy forward
  `X-Forwarded-Proto: https`.

Either way the dashboard marks its session cookie `Secure` automatically. Kenzy never
generates certs for you — TLS is a deliberate opt-in with a cert you supply. Whatever
you do, keep the dashboard off the public internet.
