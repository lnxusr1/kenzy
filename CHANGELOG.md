# Changelog

All notable changes to this project will be documented in this file.

## [3.4.0]

### Added

- **Backend services auto-register with the server.** `stt`/`tts`/`llm`/`speaker` now announce themselves to the server on startup and via a lightweight heartbeat, so they appear in the dashboard and become reachable by the pipeline **without hand-wiring `stt/tts/llm/speaker.url`** in `server.yaml`. The server resolves each service's address (using the request source IP when a service binds `0.0.0.0`), drops services that stop heartbeating, and always lets a statically-configured URL win. This fixes "I deployed the services but they never show up / the server doesn't know they exist."
- **`kenzy-deploy` auto-wires the server URL.** Deploy now derives `KENZY_SERVER_URL` for each backend service from the fleet (the host running `server` → loopback when co-located, else its address; `server_port`/`server_url` in `deploy.yaml` override) and bakes it into the service units, so config-pull and registration no longer depend on mDNS — fixing services that "start but never check in" on single-host and known-topology deploys.
- **`kenzy-deploy --listen-all`.** Binds the backend services to `0.0.0.0` (via `KENZY_BIND`) instead of `127.0.0.1`, for multi-host setups where the server must reach services on other hosts. Off by default (loopback); pair with a `KENZY_SERVICE_TOKEN` since it exposes the services on the LAN.
- **`kenzy-deploy` pip extras.** Service extras are installed automatically from a host's `services:` list. Non-service extras (e.g. `kokoro` for local TTS, `mqtt` for the HA integration) can be added via a new per-host `extras:` list — or just listed in `services:`, where they're now routed to extras instead of trying to create a (non-existent) systemd unit. `kokoro` is still auto-added when the TTS provider is `kokoro` (now read from the central `configs/services/tts.yaml`).

### Changed

- **The dashboard now binds to `0.0.0.0` by default** (was `127.0.0.1`), so it's reachable from other machines on your LAN out of the box — the common headless-server case. **This means an upgraded server's dashboard becomes LAN-reachable.** It's plaintext HTTP with a default `admin`/`password` login, so **change the password** (`kenzy-passwd` or the Settings tab); the server logs a loud security warning at startup while the default password is in use on a non-loopback bind. Set `dashboard.bind: "127.0.0.1"` to restore localhost-only. Never port-forward it to the public internet.

### Fixed

- **The "default password" warning in the dashboard didn't clear after changing the password** until the server was restarted. The in-memory flag is now re-evaluated when the password is changed, so the warning disappears immediately (and reappears if you set it back to the default).
- **Default dashboard login (`admin`/`password`) failed when the server config had no `dashboard.auth` block.** The default credentials previously lived only in the shipped `server.yaml`, so a server running from a bare or partial config — e.g. the packaged-default fallback, or one that enables the dashboard without an `auth:` block — had no credentials and rejected every login (`{"error": "invalid credentials"}`). The dashboard now falls back to the default login from the packaged `server.yaml` when the active config omits an `auth:` block, so login works out of the box (and keeps the loud "change the default password" warning). The default is **not** hardcoded in source — it's read from the shipped config.

## [3.3.0]

### Added

- **Home Assistant integration (MQTT Discovery).** Kenzy can now surface itself **into** Home Assistant — the way Frigate does — while staying a standalone product. Enable `integrations.mqtt` (new optional `mqtt` extra) and each node auto-appears in HA as a device with **State**, **Last speaker**, and **Last heard** sensors plus **Trigger**/**Stop** buttons and a **Mute** switch; no HA-side config or custom component (it uses HA MQTT Discovery). Inbound commands let HA automations drive Kenzy — `trigger`/`stop`/`volume`/`mute` per node and a house-wide `announce` — mapped to the existing server actions. The bridge tracks availability (per-node + a last-will), and the Mute switch reflects mutes made by voice or the dashboard too. Opt-in and off by default (zero overhead); broker credentials come from the environment (`KENZY_MQTT_USERNAME`/`KENZY_MQTT_PASSWORD`), and **no spoken transcripts are ever published** — only state, presence (who/where/when), and timing. Set `commands: false` for a read-only integration. (A HACS custom integration and an add-on for one-click install are planned as separate repositories.)
- **Uninstall.** `kenzy-deploy uninstall` is the inverse of install — it stops and disables the services, removes their systemd units, and deletes the venv; `--purge` also deletes the install directory (configs/.env/models/data), and `--yes` skips the per-host confirmation. The per-user installer gains a matching `install.sh --uninstall` (stop/disable the `systemd --user` units, remove the venv and the `kenzy-*` commands; `--purge` also removes the config home). Both refuse dangerously shallow paths (`/`, `$HOME`, `/opt`, …) and leave shared model caches and `loginctl` lingering untouched.
- **`kenzy-deploy` provisions into the central, dashboard-managed model.** Backend services (`stt`/`tts`/`llm`/`speaker`) are now installed in **pull mode** — their units run arg-less so they fetch their effective config from the server, which keeps them editable from the dashboard like a per-user install. A `deploy.yaml` host may set a per-host `node_id:` slug (else the node self-generates a uuid); it's baked into the node's `node.yaml` so the node has a stable, readable central record at `configs/nodes/<node_id>.yaml`. The server's central store (`configs/nodes/`, `configs/services/`) is **seeded but never clobbered** — a re-deploy only adds files the server doesn't have, so live dashboard edits survive upgrades; `kenzy-deploy --reseed install|upgrade` forces the operator's values back. (Pull-mode services need `KENZY_SERVICE_TOKEN` + mDNS or `KENZY_SERVER_URL` in their `.env`.)

### Fixed

- **A missing explicit config path no longer crashes a service.** Starting a service with a config path that doesn't exist yet (e.g. a deploy unit pointing at `{install}/configs/server.yaml` on a first deploy, before one is authored) now logs a warning and falls back to the normal resolution order — ending at the packaged default — instead of failing to start. The packaged `server.yaml` is a complete, working single-box config (discovery on, dashboard on, backend URLs pointing at localhost), so a first deploy boots with no config authoring required.

## [3.2.0]

### Added

- **Update check in the dashboard.** The Settings page now shows the installed version, the latest `kenzy` release on PyPI, and an "update available" indicator. Read-only and lazy (only queries PyPI when the page is opened, ~1 h cache, degrades gracefully offline).
- **One-click server upgrade.** When an update is available, the Settings page offers (with `dashboard.controls`) an **Upgrade server** button that runs `pip install -U "kenzy[server]"` in the server's venv — honoring your `constraints.txt` pins and pinned to the target version — then re-execs the server. The install runs in the background (it can take minutes) and reports success/failure; on success the server restarts and the dashboard reconnects to the new version.
- **One-click backend-service upgrade.** Each backend service (`stt`/`tts`/`llm`/`speaker`) now exposes a token-gated `POST /upgrade` that pip-upgrades **its own** extra (honoring `constraints.txt` + an optional version pin) and re-execs. The dashboard's Services tab has an **Upgrade** button per service (with `dashboard.controls`); the install runs in the background and reports success/failure. The upgrade helpers are shared (`kenzy.upgrade`) across the server, services, and nodes — each component upgrades only its own extra, so a shared venv converges to the full set without any host pulling another's heavy deps.
- **One-click node upgrade.** A node's Configure page has an **Upgrade** button (with `dashboard.controls`): the server sends an `upgrade` message, the node pip-upgrades `kenzy[node]` (honoring `constraints.txt`) and re-execs, reconnecting on the new version — which shows on its fleet card. Together these complete the **dashboard upgrade feature**: see what's installed and what's available, then upgrade the server, each backend service, and each node from the browser.
- **Live Home Assistant device topology.** The `home_assistant` skill now pulls your device inventory — entities, friendly names, domains, and floor/area placement — **live from Home Assistant** instead of from hand-maintained `device_ids.yaml`/`device_ids.json` files. Add a device in HA and it's voice-controllable on the next refresh, named the way HA names it. Topology is fetched with a single `POST /api/template` render (the only HA endpoint that exposes area/floor placement, and it keeps the `llm` extra dependency-free) and cached (`cache_ttl`, default 300s, stale-on-failure); device **state** is never cached and is read live only when a request needs it (status queries, relative-temperature changes). The static files remain an **offline/legacy fallback** when HA is unreachable and nothing is cached.
- **Home Assistant curation file (`curation.yaml`).** The one hand-authored input is now a small, optional curation file holding the voice layer HA can't store: per-device `aliases` and `notes`, room group-`defaults`, an `in_group: false` flag (addressable by name but out of group commands), and an `exclude` block (by entity, fnmatch pattern, domain, or area) that removes entities from voice control entirely — e.g. smart-plug status LEDs that masquerade as `light` entities. Keyed by stable HA entity IDs.
- **`kenzy-ha-devices` CLI.** Prints the live `floor → area → domain → entity` tree with each entity ID and whether it's included or excluded (and why), to help author `curation.yaml` and verify exclude rules. Loads `llm.yaml` locally (no server pull) plus `.env` for `HA_API_KEY`.
- **Dashboard Home Assistant tab.** A new **Home Assistant** tab is a GUI editor for `curation.yaml`: a tree of your live HA devices with per-entity alias / note / *in groups* / *exclude* controls, per-room *default* toggles, and bulk exclude patterns/domains/areas. Saving validates and writes the file and refreshes the topology cache immediately — no restart. Backed by token-gated `GET`/`POST /ha/curation` on `kenzy-llm` (proxied by the dashboard, edits gated by `dashboard.controls`).

### Fixed

- **Dashboard inline code spacing.** Inline `code`/term spans that wrapped to a new line in the source (Activity, Settings, Speakers, and the calibration wizard) lost the space before them, so text ran together mid-sentence. They now render with the proper spacing.

## [3.1.0]

### Added

- **PyPI install path.** Default service configs, `.env.example`, and the built-in skills now ship inside the package; `kenzy-init` scaffolds a config home (`~/.config/kenzy`) from them. A new `kenzy.config` resolver finds configs via `$KENZY_HOME` → `./configs` → `~/.config/kenzy` → packaged default, so services run from a plain `pip install` with no source checkout.
- **mDNS discovery.** The server advertises `_kenzy._tcp` (`python-zeroconf`); a node with no `server_url` auto-discovers it, and `room_id` defaults to the hostname. An explicit `server_url` still skips discovery.
- **Config-pull over WebSocket.** On connect, `hello` carries the node's identity, audio capabilities, and an optional join `token`; the server replies with the node's effective config (`node_defaults` + per-node `configs/nodes/<node_id>.yaml`). An optional `discovery.token` gates the join.
- **Zero-config nodes.** `node.yaml` is now **bootstrap-only** (identity + how to reach the server + early logging). A node **blocks until the server pushes its config** before initializing audio (no boot-from-cache); hardware keys (audio device, sample rates, wakeword models, sounds) are applied on that first pull, while live-tunable keys and the room name apply immediately on every push. So a room device runs with an essentially empty local file and is configured entirely from the dashboard.
- **Stable node identity (`node_id`).** Each node generates and persists a stable `node_id` (on first run, or assigned at install via `kenzy-init --node-id`); the server keys the registry, per-node config overrides, and all controls on it. The **room name is server-owned** — stored in the per-node override, pushed on connect (so a node can be pre-seeded/renamed before it ever boots), and sent to the assistant as context. Pre-split, room-named override files auto-migrate to the `node_id` key on first connect.
- **Centralized config for backend services.** The server is the config authority for `stt`/`tts`/`llm`/`speaker`: an always-on, token-gated `GET /config/<service>` serves each service's effective config (packaged default + server-owned `configs/services/<service>.yaml`, secrets stripped). Services discover the server (mDNS or `KENZY_SERVER_URL`), pull their config at boot with retry/backoff (so start the server first / `After=kenzy-server`), and expose a token-gated `POST /restart`. Edited from the dashboard's **Services** tab. `zeroconf` added to the `stt`/`tts`/`llm`/`speaker` extras.
- **Full-depth log viewer.** Console verbosity (`log_level`, default `info`) is decoupled from how deep the dashboard log buffer captures (`log_capture_level`, default `debug`), so the viewer can show DEBUG even when the console shows INFO. A new `TRACE` level carries hot-path/per-frame node logs; the Logs tab gains a TRACE filter and an on-demand **Capture TRACE** button that boosts a node to TRACE for a chosen window and auto-reverts. Node log levels are live-tunable from the dashboard.
- **Opt-in web dashboard** served by `kenzy-server` (`dashboard.enabled`, off by default — zero overhead when off): a full fleet manager — login auth (`kenzy-passwd`), live fleet/health view, per-node config editor with room rename, a **Services** editor for backend-service config (with restart), Trigger/Stop/Restart, TTS announcements, a pull-based log viewer, and a Settings page (system info, a scoped server-config editor, password change). Config editors use typed fields — dropdown choosers for fixed-value options and number inputs for numerics. Secrets are never served.
- **Voice broadcast (announcements).** Say "tell everyone dinner's ready" and Kenzy speaks it in every room. A built-in `announce` skill rides a new **LLM→server actions** channel (`ProcessResponse.actions`): a skill queues a server-side action the LLM service can't perform itself, and the server actuates it (here, the existing `announce()` — resolving room names to nodes and excluding the asking room). The server also injects the connected room names into each LLM request so the model targets real rooms.
- **Intercom (live two-way room-to-room calls) with a consent gate.** Say "call the living room" (the `connect_room` skill) and the server **rings** the target room; it bridges live two-way audio **only after someone there says "yes"** (spoken consent, transcribed via STT, default-deny on silence/ambiguity/~25s timeout — no auto-accept). Once connected, the server relays raw PCM between the two nodes and each plays the peer live; a **wake word at either end ends the call immediately** on both. New protocol messages `call_request`/`call_cancel`/`intercom_start`/`intercom_end`, a node `RINGING`/`INTERCOM` state machine, and a thread-safe streaming-playback ring buffer in the node's audio player. **Requires a speakerphone with hardware echo cancellation.**
- **Dashboard pipeline observability (Activity tab).** A new **Activity** tab shows recent voice interactions — transcript, identified speaker, spoken response, whether the deterministic fast path or the LLM handled it, and a per-stage latency breakdown (STT/speaker → LLM → TTS) with total response time — plus header stats for fast-path hit rate and average latency. Live-updating, bounded in-memory (no disk), and gated by `dashboard.logs` since records include transcripts.
- **Auto-wired peer service URLs.** Dependent services no longer need to duplicate another service's URL: the server injects the endpoints it already knows into the config it serves (today, `tts.url` into the speaker config, used by enrollment voice prompts), so you configure each backend's address once in `server.yaml`. A local value still overrides it for multi-host setups. `kenzy-enroll` pulls the TTS endpoint from the server when it isn't set locally.
- **Voice speaker enrollment.** Enroll a voice by speaking to a node — "Hey Kenzy, enroll me as Alice" — instead of only the `kenzy-enroll` CLI: Kenzy prompts for a few sentences, captures them through that room's mic, and registers them with the speaker service. **Off by default** (`allow_voice_enroll` in the speaker config — toggleable from the dashboard's Services → speaker, read live by the server); when enabled, anyone in earshot can enroll, so it's a deliberate opt-in (the docs warn this can bypass speaker-gated actions and recommend the CLI for people who can unlock things). Both the voice path and the CLI read the **same configurable `enroll_prompts`** list from the speaker config (one sample per prompt), so editing it in the dashboard updates both. `docs/speaker-enrollment.md` documents both paths.
- **Guided audio setup wizard.** A node's Configure page has a **Set up / calibrate audio** button that opens a step-by-step wizard (device → silence → wake word) fed by an on-demand, time-boxed telemetry stream (off / zero-overhead unless the wizard is open). It picks the mic/speaker (restarting the node and waiting for it to reconnect so calibration measures the right device), then suggests `silence_rms_threshold` and `wakeword_threshold` from live meters (applied live) and a `wakeword_vad_threshold` (applied with one restart at the end) — replacing trial-and-error YAML edits. You can run the whole flow or jump to one step to recalibrate it; raw audio keys remain under an "Advanced" disclosure. A headless **`kenzy-node --calibrate`** runs the same measurement on a node with no dashboard and prints the suggested values to apply server-side.
- **Dashboard audio-device picker.** A node now probes its audio devices (reusing the `kenzy-devices` scan) and reports them to the server, so the per-node config editor offers a dropdown of the room's mics/speakers that fills in `audio_device` and the right sample rates — no more hand-running `kenzy-devices` on the box. Combined with non-fatal audio init, a node with a wrong/missing device can be corrected entirely from the dashboard (pick device → Save → Restart).
- **Per-node volume + mute.** Set a room's playback volume (`volume`, 0–100) from the dashboard or by voice ("turn it up", "set the volume to 40") — applied live via config-pull, affecting TTS, intercom, and announcements. Mute/unmute ("mute"/"unmute", or the dashboard toggle) silences playback **except the wake-word ready chime**, which stays audible at a floor level so you can tell the device is listening and knowingly unmute. Volume persists; mute is a transient runtime toggle (a node comes back un-muted after a restart). A built-in `volume` fast-intent rides the LLM→server actions channel (`set_volume`).
- **Security hardening (first pass).** Dashboard read endpoints now **require login** like the mutations did (previously `/api/state`, node config, logs, and the transcript-bearing `/api/sessions` were readable by anyone who could reach the bind); static assets stay public. **Secure-by-default join token:** `kenzy-init` now generates a `discovery.token` for a server/all install and wires the same value into server.yaml, the co-located node.yaml, and `.env` (`KENZY_SERVICE_TOKEN`), so node joins, the `/announce` + `/config` endpoints, and service-to-service calls are authenticated out of the box; the dashboard's **Settings → Node provisioning** shows the token (copy button) so you never memorize it, and `kenzy-init`/`install.sh` take `--token` to paste it on a node (or share one across hosts). The node join-token check is now constant-time and an explicitly-supplied `node_id` is validated at registration (rejecting unsafe/path-like ids). The speaker service validates every speaker name (enroll/delete/rename) against path traversal. And the dashboard warns — at startup and with a Settings banner — when it's still using the default `admin/password`. **Defense-in-depth (P1):** the dashboard's WebSocket/mutation channel rejects cross-site handshakes (Origin must match Host) and, when `dashboard.allowed_hosts` is set, off-list Hosts (DNS-rebinding defense); the session cookie gains `Secure` automatically when served over TLS (`X-Forwarded-Proto: https` — a reverse proxy is the supported HTTPS path); inbound WebSocket frame sizes are capped, a single capture buffer is bounded (~2 min), and new connections are rate-limited per source IP; and `kenzy-deploy` now shell-quotes interpolated config values. See `design/security-hardening.md` (P0 + P1).
- **Dashboard speaker management (Speakers tab).** A new **Speakers** tab manages the enrolled voice profiles held by `kenzy-speaker`: it lists each voice with its sample count and the service's identify threshold, and (with `dashboard.controls`) lets you **rename** or **delete** profiles. It also offers **Enroll from a room** — start voice enrollment on a connected room node without recording audio in the browser; as an authenticated operator action it bypasses the `allow_voice_enroll` earshot gate. The speaker service's `GET /speakers` now reports per-speaker sample counts and gains a `POST /speakers/{name}/rename` endpoint.
- **Dashboard skill registry (Skills tab).** A new **Skills** tab lists the skills and deterministic fast intents loaded by `kenzy-llm`, each with a one-line description and an invocation count, and (with `dashboard.controls`) lets you **enable/disable a skill live — no service restart**. Skills are now loaded-but-gated, so a toggle takes effect immediately and is persisted to `configs/services/llm.yaml` (`skills.disabled`); disabling a skill also disables its same-named fast intent. New token-gated `GET`/`POST /skills` endpoints on the LLM service back it.
- **PyPI packaging metadata.** Completed the `[project]` metadata for a proper PyPI listing — long description (README), MIT license, author, keywords, trove classifiers, and project URLs (homepage, docs, repository, changelog). The built sdist + wheel pass `twine check`, and a clean-venv install was verified to run standalone.
- **Per-host version reporting.** Every component now reports its installed `kenzy` package version — backend services on `GET /health`, nodes in their `hello`, and the server already did — and the dashboard surfaces it (per-node card + service chip). A shared `kenzy.kenzy_version()` helper backs it. This is the visibility groundwork for the upcoming dashboard upgrade feature (see what's installed where, and what came back after an upgrade).
- **Per-host dependency pins (`constraints.txt`).** The config home now holds a pip constraints file that Kenzy honors on install **and every future auto-upgrade**, so a host that needs a specific dependency version (e.g. `transformers` for a particular GPU/model) keeps it across upgrades instead of having it silently moved. `kenzy-init` scaffolds a template; `install.sh --constraints FILE` (or `KENZY_CONSTRAINTS`) seeds it; `kenzy.config.constraints_path()`/`pip_constraint_args()` expose it for the install path and the planned upgrade feature. `kenzy-deploy` honors the same pattern — a `constraints:` file (or an auto-detected `constraints.txt` at the config-root) is pushed to each host and passed with `-c` on install and `kenzy-deploy upgrade`, in both source and pypi modes. If a release can't satisfy a pin, the upgrade fails loudly on that host rather than breaking it.
- **Scoped server self-config editor.** The dashboard's **Settings** page can now edit a safe subset of the server's own config (dashboard `logs`/`controls`, each backend's `url`/`timeout`, the unknown-speaker label, mDNS `discovery.enabled`/`instance`) — writing a `server.local.yaml` override layered over your hand-edited `server.yaml` (comments preserved) and **restarting the server** to apply. Lockout/secret-sensitive keys (bind/port, dashboard bind/port, login credentials, `discovery.token`) stay file/CLI-managed and are not editable. It requires login but not `controls` (since it's how `controls` gets turned on). The dead `dashboard.tuning` flag was removed.
- **`kenzy-deploy` install modes.** New `install_mode: source|pypi` (per host or global) plus `--local` (force source) and `--version` (pin a PyPI release); pypi mode pushes only `configs/` and installs `kenzy[extras]` from PyPI.
- `kenzy-init` command and the `kenzy.discovery` module; `zeroconf` added to the `node` and `server` extras.

### Modified

- **Prompt `kenzy-node` shutdown on Ctrl+C.** The node now handles SIGINT/SIGTERM via the event loop: it cancels cleanly, signals any in-flight mDNS browse to return at once (a blocking browse otherwise delayed exit until the discovery timeout, since the worker thread is joined at interpreter exit), and tears down the audio streams **off the main thread with a bounded join** so a slow/hanging PortAudio/ALSA `close()` can't stall shutdown. A daemon force-exit watchdog remains as a last-resort backstop if anything else wedges in a blocking C call (where repeated Ctrl+C would otherwise have no effect).
- **Resilient node audio init.** A failure to start audio (e.g. a misconfigured `audio_device`) is no longer fatal: the node tears down any partial streams, reports `status{audio_ok:false}` to the server, and **stays connected with its command loop running** so you can correct the device and Restart it from the dashboard (previously it crashed before becoming controllable). The fleet view flags such nodes with an "audio failed" badge.
- **`install.sh`** rewritten as a per-user PyPI installer (profiles, `--no-apt`, `--package` for local wheels/sdists, `--version`, `--node-id` for a node's stable identity, config-home scaffold, `kenzy-*` linked into `~/.local/bin`, `systemd --user` units ordered `After=kenzy-server` for backend services) — no longer a git-clone bootstrapper.
- Built-in skills moved into the package (`kenzy/llm/builtin_skills/`); the skill loader now loads built-ins first, then your `skills.dir` overlay (same-named files override built-ins), with `skills.disabled` applying to both.
- `kenzy-deploy` re-roots on the `deploy.yaml` location (config-root) instead of `pyproject.toml`, so pypi-mode deploys work without a source tree.
- `build_pypi.sh` builds via `python -m build` (the v3 project has no `setup.py`).
- **Wake word.** The bundled default is now a single model, `hey_ken_zee.tflite` (the previous `hey_kenzie.tflite` / `ken_zee.tflite` models were removed); custom `wakeword_models` still override it.
- Documentation updated throughout (getting-started, configuration, architecture, deployment, skills) and a new **Dashboard** guide added; centralized config, zero-config nodes, and the log viewer documented across the node/server/service references. Added room-node hardware guidance (tested boards: Orange Pi Zero 3/3W or Raspberry Pi 3/4/5) and a recommendation to use a speakerphone with hardware AEC. Docs site restyled to the Kenzy palette via `extra_css`.

## [3.0.0]

### Added

- Speaker recognition / Voice Identification
- Deploy scripts and helpers
- OpenWakeWord detection
- LLM backed intent processing and tool calling
- Optional cloud STT using openAI (local STT via faster-whisper is still the default)
- Deterministic fast-path skill layer (`@fast_intent`): common commands resolve locally with no LLM round-trip, falling back to the LLM automatically when unmatched
- Wake word VAD gating (`wakeword_vad_threshold`) using openwakeword's Silero VAD to suppress false activations on near-silence/noise
- SKILL: Time/date queries answered instantly via the fast path

### Modified

- **REWRITE**: Application rewritten as a smart speaker module
- Implemented microservices architecture for nodes, servers, llm, stt, tts, and speaker identification
- Speaker nodes now only perform wake word activation and VAD and stream audio to server (Speaker can now run on Raspberry PI Zero 2W)
- Local TTS moved from speachT5 to Kokoro
- VAD moved to threshold RMS values
- SKILL: Moved to National Weather Service free APIs for weather skill
- SKILL: Stock ticket updates
- SKILL: Random number generator
- SKILL: News via RSS
- SKILL: HomeAssistant control now resolves common commands deterministically (padacioso intent parsing + rapidfuzz device matching) before falling back to the LLM resolver; adds optional per-room device overlay (aliases, default groups, exclusions), on/off group asymmetry, explicit-room scoping, and speaker-gated lock/unlock

### Removed

- Image processing (see kenzy-image for similar functionality)
- Dashboard

## [2.1.5]

### Added

- Added clean_text (text used during intent processing) to skills intent calls.
- Updated WatcherSkill and ThankYouSkill for basic improvements

### Modified

- Updated all links from Kenzy.Ai to Kenzy.DEV due to domain name change

## [2.1.4]

### Added

- Added skills min & max app versions and check function to control which skill manager versions the skill is compatible with
- Ability to control when kenzy is activated (or deactivated) via skills
- New skill updates:  
  - HomeAssistant version increment
  - MuteSkill added

## [2.1.3]

Bugfix-only version

### Modified

- Fix for HA triggering
- Fix for managing skill versions
- Fix for skill reload

## [2.1.2]

### Added

- Dashboard updates for sorting lists, viewing device details, and developers section
- `log_level` device option for skillmanager for log visibility in dashboard
- Added acknowledgement sound when text-to-audio does not exist in cache.
- Added info section on dashboard for devices list
- Added ability to start/stop child devices via control panel

### Modified

- Fixed start/stop calls to TTS library
- Fixed `play_wav_file` to allow for files outside of program path
- Fixed offline setting for transformers up through v4.11
- Fixed bug in skillmanager skill download to stop running skills before replacing

## [2.1.1]

### Added

- Activity section on dashboard for info from cameras
- Added external player call for playing wave and speech files

### Modified

- Fixed bug in multi-location setup where all nodes activated when any node received command
- Fixed bug in locations count on dashboard
- Fixed errant text in dashboard about page
- Fixed compatibility in setuptools deprecation notices
- Fixed bug in ASK that forced timeout to occur before moving to next command

## [2.1.0]

### Added

- Data capture to kenzy.skillmanager.device -> history for all in/out of skill manager
- Data capture to kenzy.skillmanager.device -> data for all current data (for reference in skills)
- WatcherSkill for articulating what is captured on one or more kenzy.image devices
- Callback Triggers for skills for non-speech activity (like kenzy.image)
- Ability to set a custom name for built-in devices

### Modified

- Locks, Covers, and Lights can be disabled/enabled as a group in the HomeAssistantSkill
- Using the keyword "all" or plural form of lights, lamps, or fans in HomeAssistantSkill will toggle all lights/fans/lamps in the specified area
- Stopped `collect()` from sending data until service registration is complete
- Integrated kenzy-skills library on github for skills inventory and download
- Updated docs for individual skills

## [2.0.3]

### Added

- Added a default configuration for the base kenzy startup (saved to .kenzy/config.yml).
- Core support for versioning skills. (use `self._version` to set version number).
- Added `--skip` and `--only` options to skip or include device configs in provided file.
- New skill option for WeatherSkill (requires API key from [openweathermap.org](http://openweathermap.org))
- Added option to set default value when getting settings in skills

### Modified

- Changed startup to use Multiprocessing instead of Threads for each device main runtime
- Added ThreadingMixIn to HTTPServer (oops!)
- Set default of "Kenzy's Room" and "Kenzy's Group" for location and group respectively
- Improved responses to the "How are you?" prompt.

## [2.0.2]

### Modified

- Fixed bug in skillmanager.device.collect
- Fixed bug in core.KenzyRequestHandler.log_message
- Fixed bug in *Cameras* count on dashboard

## [2.0.1]

### Added

- Settings handler for consistency when customizing per device settings
- GPUs can be leveraged for torch and cuda enabled models
- Added options for saving video of detected people
- Directly incorporated kenzy_image into kenzy.image.core.detector
- Added reloadFaces logic to kenzy.image.detector (formerly of the kenzy-image package)
- Added voice activation with configurable timeout
- Added multi-model support for speak-to-text
- Added configurable timeout for SSDP client requests
- Added extras helpers to extract numbers from strings and convert numbers to english words.
- Added clean text routine for supporting the rich output from OpenAi's Whisper model
- Basic support for simultaneous actions (such as two listener+speakers in two rooms connected to same skillmanager)
- Object recognition, Face detection, and Face recognition with optimizations to minimize processing time with support for multiple models
- Configurable saving of videos based on object detection alerts

### Modified

- Settings/Configuration files can now be stored in JSON or YAML files
- Moved watcher to ```kenzy.image.device.VideoReader```
- Moved listener to ```kenzy.stt.device.AudioReader```
- Moved speaker to ```kenzy.tts.device.AudioWriter```
- Restructured devices to allow for direct calls for "main" in each of image, stt, and tts
- Split out detector/creator processes for each of hte core functions into their own modules (e.g. kenzy.image.detector, kenzy.stt.detector, etc.)
- Moved all devices to their own HTTP server module when run as clients
- Fixed the UPNP logic so that it honors the full UPNP spec for control interface lookups
- Updated skills intent function signature to include ```**kwargs``` for additional values like raw text captured
- Fixed the context inclusion and usage for action/response activities (uses "location" for relative responses)
- Completely overhauled dashboard

### Removed

- Dropped support for PyQt5 panels
- Dropped direct support for Kasa smart switch/plug devices
- Dropped unnecessary libraries (urllib3, netifaces)
- Dropped support for MyCroft libraries "mimic3" (created forked version of padatious for future internal support)
- Dropped direct support for Raspberry Pi due to hardware limitations

## [1.0.0]

### Modified

- (MINOR) Fixed bug in autoStart conditions for devices preventing devices from honoring the setting when set to ```False```
- Moved RaspiPanel into "panels" module
- Set the running app to be PyQt5 specific
- Adjusted the startup arguments for GenericContainer to be non-specific
- Fixed build cleanup process
- Set the PyQt5 example panel to be disabled by default (but available to 'start' in web UI)

## [0.9.9]

### Modified

- Listener error trapping for invalid audio devices to report stopped status on failure
- Watcher error trapping for invalid camera devices to report stopped status on failure
- GenericContainer now saves core init() args to ```self.config``` and initialize() args to ```self.args```

## [0.9.8]

### Added

- Downloadable installer script
- Installer script documentation
- Logo in docs

### Modified

- (CRITICAL) Fixed skill inclusion breaking runtime due to missing "create_skill()" attributes
- Cleaned up documentation on inclusion of libraries (added python3-venv and removed traceback)
- Corrected documentation on PyAudio library installation
- (CRITICAL) Fixed inclusion of missing files in PyPi build

- (Sorry about the version increments... still getting use to PyPi.org)

## [0.9.2]

### Added

- Added ```nickname``` option to devices/containers

### Modified

- PyPi integrations updated and streamlined build
- Modified versioning storage/processing
- Updated to a basic README for PyPi download page
- Multiple bugs fixed in KasaPlug for local, direct plug access
- Bug fix for isAlive() to is_alive()

## [0.9.1]

### Added

- Dependency on stt (a.k.a. "coqui" which is a replacement for deepspeech)
- Added new parameters for Speaker to be able to integrate with mimic3

### Modified

- Renamed all objects to support "kenzy" (and related variants)
- Updated "ask" function to start timeout after the originating utterence ends (rather than when it starts)
- Documentation for installing on Ubuntu 22.04 LTS (pyaudio and libfann source package installation workarounds)
- Download Models option now defaults to tflite format and pulls the Coqui base models from the Coqui Model Zoo
- Moved all request/response processing into Skills and removed hardcoded responses
- Removed hard dependency on padatious library
- Fixed bug in skills processing for multiple intents
- Updated device callbacks to use the GenericDevice naming convention
- Updated device settings to allow for store/update on the fly
- Updated container settings to allow for store/update on the fly
- Adjusted where version information is stored

### Removed

- Dependecy on deepspeech
- Documentation dependency on padatious (libfann related issues for auto-build in readthedocs API)