# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [4.3.1]

### Security

- **The legacy service-auth paths are gone — token-proof only.** During the 3.11/3.12 server-authority migration the shared fleet token stopped riding the wire — nodes prove possession with an HMAC signature in `hello.auth`, and service-to-service calls sign an `X-Kenzy-Auth` header — but servers still accepted the old raw forms so a mixed-version fleet kept working. That deprecation window is now closed: with a token configured, it can **only** be proven by signature. A raw `Authorization: Bearer <token>` on a service call and a raw `token` field in a node's `hello` are both refused (401 / connection closed), and a relay that can see the traffic never learns anything replayable. Running *without* a token is unchanged — auth stays opt-in, and tokenless meshes behave exactly as before — as are the dashboard's own login bearer and the `KENZY_SERVER_TOKEN`/`KENZY_SERVICE_TOKEN` env names; this only removes the clear-text-secret acceptance. The one operational consequence is the intended one: with a token set, every node and service must be on ≥3.12, since an older host would present the raw form and is now rejected.

## [4.3.0]

### Added

- **"Play some jazz" — music by name, through Music Assistant.** A new fast intent hands any *play/put on/listen to …* phrase straight to Music Assistant's `play_media`, which resolves the artist/album/track/playlist itself — Kenzy carries the words and picks the right room's player (the room you name, else the room you're in, else the house's only one; a room with no player gets an honest answer). MA players are recognized by **integration ownership in HA's entity registry** — never by name — so renaming entities can't break targeting — and the guard cuts both ways: **control verbs can never touch an MA player**. Adding the MA integration imports players named after the devices they wrap (two "Office TV" entries — field bug: "turn on the TV" actuated the MA queue frontend and reported success), so name/type/group resolution is now blind to MA players entirely; only play-by-name and the transport verbs (pause/skip/volume, which want the thing that's *playing*) may target them. Titles survive intact ("Dancing in the Dark" is never mistaken for a room called Dark); houses without Music Assistant never see the intent fire. The `play_music` tool covers the fuzzy tier with the same honesty, and pause/skip/volume on a playing MA player work through the existing media transport verbs.

## [4.2.1]

### Added

- **The Settings page grew a Controls section — and the server itself can now be disabled from the dashboard.** Restart moved out of the config editor into a Controls card matching every service page, joined by **Disable server** on systemd installs: `disable --now` for the whole stack — every room goes quiet and the dashboard goes with it, which is exactly why the typed confirmation shows you the recovery one-liner (`systemctl --user enable --now kenzy-server.service` on the server host) before you pull the plug. Dev checkouts say honestly that enable/disable doesn't apply instead of showing a dead button.
- **The server's own optional extras got feature chips — the last extras without an Install button.** The Settings → System card now shows `mqtt` (the HA MQTT bridge) and `sound` (MP3/OGG/FLAC decode) with the same honest states as the service chips — including *enabled in config but NOT INSTALLED* — and a one-click Install that fills the dependency (constraints honored, no version moves) and restarts the server. With these two, every optional pip extra in the stack is installable from the dashboard.
- **Nodes can be disabled from the dashboard too.** Each node's Controls row gains **Disable node** (next to Restart) when the node runs as a systemd unit: the server sends a new `disable` protocol message and the node shuts its own unit off (`disable --now` — restart policies can't resurrect it), going quiet until you run `systemctl --user enable --now kenzy-node.service` on its host — which the confirmation tells you before you click. Nodes report their unit state in `hello`, so the button only appears where it actually works; services already had Disable/Enable since 4.1, so with the server added, every part of the stack can now be switched off from one place.

### Fixed

- **Settings page: the "Restart server" button no longer stretches to full width.** The ghost-button base style flexes to fill its row (it was designed for the sidebar), and the Settings actions row only pinned the primary button's width — the restart button now sizes to its label and wears the same red danger styling as every other Restart button.

## [4.2.0]

### Added

- **Skills can ask questions — `ask()` (the 4.2 headliner).** A skill that needs the user's answer mid-flow finally has a first-class path: `reply = await ask("Should I create a list called Groceries?")` speaks the question, holds the room's floor (no wake word needed to answer), and resumes the skill exactly where it paused — whether it's a fast intent or a tool call deep in the model's loop. The wake word *always* cancels a pending question (the household's universal escape hatch — the skill sees `None` and its result is discarded), the reply window is the node's own dialog timeout, and the answer carries the *answerer's* identity so permission-gated skills can re-check who's actually talking. Questions chain naturally, and continuations are deliberately in-memory and mortal — a service restart forgets them and the room quietly moves on.
- **Enrollment is an ask() conversation now — and ask() learned to hear.** `ask_audio()` returns the user's raw spoken reply (the record-after-the-tone capture) instead of a transcript, and voice enrollment was rebuilt on it as the proof of generality: the whole prompt/sample/retry/confirm flow is one readable coroutine in the enrollment skill, replacing the server's hand-rolled session state machine (prompt loops, retry counters, inactivity timers — all gone). Same behavior at the microphone — each prompt spoken then the record tone, "I didn't catch that" retries (an expired window arrives as an empty sample), the wake word bails out, person-first adoption on the first stored sample — plus one improvement: the enrollment sentences now come from the speaker service's own `GET /enroll/info`, so the dashboard-editable prompts are read exactly where they're owned.
- **ask() works across rooms — and intercom consent rides it.** `ask(prompt, room="kitchen")` speaks the question in *another* room, holds that room's floor for the answer, and brings the outcome back to the asker — the asker hears an announcement ("Calling the kitchen.") plus ringback while the question travels. Intercom consent is its first user: the whole ring/consent/decline flow is now the `connect_room` skill's own conversation, replacing another server-side pending-state machine. Semantics preserved and sharpened: default-deny (only a clear spoken yes bridges), the target's wake word or silence comes back to the caller as "No answer from the kitchen" instead of dead air, anything else is a polite decline — and the audio bridge itself stays server-owned.
- **The People page updates itself the moment memory changes.** Any memory or lockbox mutation on kenzy-llm now pokes the server (debounced, token-optional like every service call), which pushes a data-less `{"type":"memory"}` to dashboard browsers — a fresh "remember…" appears on an open People page in a couple of seconds instead of waiting for the poll (now a slow fallback, like the Scheduled tab's). The plumbing — the server's always-on `/notify` hook — is generic for future live views.
- **`memory.classifier_keep_alive`** — pin the classifier/consolidation model resident in Ollama from Kenzy's own config (`"-1"` forever, `"30m"` for a while), so the warm-model fix travels with the deployment instead of living in the Ollama host's environment. Only ever sent to `ollama/*` models; blank sends nothing.
- **Memories are editable from the People page.** Each fact gained an **Edit** next to Forget: fix the wording (an edited fact re-enters the duplicate-merging pass, so a reword coalesces naturally), change the tier — private / about them / household-shared — and set a **retention window** (keep forever, or forget after 30/90/365 days; expiring facts wear an "expires in…" badge and the maintenance sweep removes them on schedule). The re-tiering half closes the old "settable via the API only" gap.
- **"Is Mom home?" — presence, on demand.** A new built-in skill reads Home Assistant's person entities live when someone asks — "is Dad home?", "where's Alice?", "who's home right now?" — through the `ha_user` link on the People page (zero new configuration). Deliberately read-on-demand, never ambient, and gated to recognized voices: who's home is household information. Unlinked people get an honest pointer to the People page instead of a guess.
- **Suggest capture mode is real.** The third memory-capture option now works: with Suggest on, Kenzy notices a durable personal fact mid-conversation ("my barber is called Vinnie") and *asks aloud* — "Want me to remember that your barber is called Vinnie?" — storing only on your spoken yes, with a `suggested` source tag on the People page and the same quarantine pipeline as every write. Built on ask(), which is exactly why it had to wait for 4.2.
- **Chimes grew into a sound system.** Play *any* sound in your library — not just the bundled chimes — in one or more rooms, from HA automations: `data/sounds/` in the config home is always available (and rides backups), and `sounds.dirs` in server.yaml adds more roots (a NAS mount, a shared library; subfolders fine). Sound names are resolved **only** inside those roots — traversal and absolute paths are rejected outright, and the roots list stays file-managed on purpose: it *is* the security boundary. New alongside: `repeats: N` (count-shaped looping next to the duration-shaped `seconds`), an HTTP **`/chime`** twin of `/announce` for broker-less HA (`rest_command`), and **MP3/OGG/FLAC support** — decoding happens server-side via the optional `av` package (`pip install 'kenzy[sound]'`), so nodes keep receiving plain audio and Pi-class boxes never touch a codec. A typo'd sound name is an honest 404, not a silent nothing.
- **List confirmations ride ask() now.** "Should I create one called Shopping list?" and "Delete it for good?" are real suspended conversations instead of per-room pending-state with timers — same guarantees (nothing is ever created or deleted without the spoken yes; a wake word or an expired window changes nothing), less machinery, and the LLM-tier `delete_list` tool now runs its own confirmation instead of asking the model to relay one.

### Changed

- **The model gets more room to work: `max_tool_iterations` now defaults to 10** (was 5) — multi-step requests ("check the weather, then add an umbrella to the list if it's raining") stopped hitting the loop ceiling mid-plan.
- **Duplicate memories now merge within about a minute.** The mechanical maintenance sweep (expiry, exact dedupe, tombstone retention) runs every 60 seconds by default instead of hourly — it's cheap, runs are sequential (a slow pass just delays the next tick), and the write-kicked semantic pass already worked this way.
- **`speech_min_ms` now defaults to 400** (was 500) — the silence detector arms sooner, so short single-word commands ("Stop.") end their capture promptly instead of waiting out the no-speech timeout. (The shipped docs already said 400; code and the shipped `node_defaults` now agree.)
- **Docs use `https://` dashboard addresses** — TLS is the installer default now, so every `http://<server>:8770` in the docs was updated (your browser will warn once about the self-signed certificate; that's expected).

### Fixed

- **A pre-release adversarial review of the ask() plumbing closed four gaps** (none shipped in a release): a server-side action queued *before* a skill's first question could be dispatched twice (once with the question, again with the final answer); an action queued *between* questions — enrollment's link-voice-to-person step — was only delivered at the very end, so walking away mid-enrollment could leave a voiceprint with no person record (actions now ship with the very next spoken turn, so the link lands the moment the first sample is stored); the ten-minute backstop that clears forgotten questions now fully unwinds the paused skill instead of leaving it adrift; and the internal enrollment trigger is no longer reachable as typed text through the HA Assist channel.
- **A long enrollment can't be cut off by the dialog turn limit anymore.** Ask-driven conversations (enrollment's five prompts plus retries) now have their own generous ceiling instead of sharing `dialog.max_turns` with ordinary back-and-forth chat, which capped the flow at six exchanges — "floor not held" mid-enrollment.
- **A GPU-broken STT no longer fails every request forever — and it tells you.** Field find (4.2.0): setting `whisper.device: cuda` without the CUDA/cuDNN libraries starts cleanly (the model builds without touching the GPU kernels) and then 500s on *every* transcription — with nothing in the dashboard log viewer, because the exception died inside the web layer. Now a local transcription failure is logged where you can see it, and a GPU inference failure **rescues once onto the CPU** and keeps serving (loudly — the log says what to fix, `/health` reports `device_fallback: true`, and the GPU is never silently retried). The root cause was an upgrade hazard: `ctranslate2` (under `faster-whisper`) moved its requirement from cuDNN 8 to cuDNN 9 at 4.5, so upgrading Kenzy could silently break a working GPU setup. The easy repair is now built in too: Kenzy **preloads pip-installed NVIDIA runtimes** — `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` into the venv is the whole fix, no `LD_LIBRARY_PATH`, no unit edits (see the new GPU section in the STT docs).
- **A flood of memory-change pokes can't stampede the dashboard.** The People page's live-update push is coalesced server-side (1s) as well as at the source, so the token-optional `/notify` hook can't be used to fan out per-request broadcasts to every open browser.

## [4.1.0]

### Added

- **The lockbox — Kenzy keeps secrets the way you'd hope (v4.1 headliner).** Codes, passwords, and combinations get different *mechanics*, not just a stricter rule: a lockbox secret is **encrypted on disk**, **never enters any language model** (not context, not consolidation — each secret is a key/value pair: the model sees only non-sensitive keys and answers with a placeholder that Kenzy fills in deterministically after the model is done, so any phrasing works and the value never transits a model in either direction), and is owner-only forever. "Remember this secretly: …" vaults on the spot — the utterance never reaches a model — and the People page shows a 🔒 label and date — the text appears only on an explicit, admin-gated **Reveal** click that hides itself again after 30 seconds. Remove-completely erases a person's secrets along with everything else.
- **Secrets are never spoken through cloud TTS.** Asking for a lockbox secret with a cloud speech provider configured (the default OpenAI voice) gets an honest deflection to the dashboard instead of sending the value to the provider inside the reply audio; with local speech (`kokoro`) it's spoken normally. The TTS service reports its locality on `/health` and the server passes it per-request, fail-closed.
- **Backups restore your secrets; exports answer completely.** The backup archive now carries the lockbox *and* its key by default — a backup's job is to bring everything back (untick "Include the lockbox key" for a shareable, ciphertext-only archive; TLS keys always stay out, `.env` stays out unless opted in). A person's "Export their data" download likewise includes their lockbox entries by default — the same access Reveal already grants — with an untick for a shareable file.
- **Restating a secret updates it.** The lockbox is key/value: "the door code has changed to 6,000" replaces the old door code under the same key — deterministically, no model, instantly. Value extraction understands the spoken verb family (is / changed to / has been updated to / reset to / …) and falls back to the last code-shaped chunk for verbless phrasings ("door code 4593"); digit payloads with commas/periods ("6,000", "1.2.3.4") classify as secrets instead of landing in the review queue. And the lockbox never destroys what it can't read: if the encryption key is regenerated (or a restored archive arrives without one), the old ciphertext is preserved aside with a loud log instead of being overwritten — restore the matching `lockbox.key` beside it to recover; the service also logs its secret count at startup so a wiped lockbox is visible immediately.
- **Every memory write is now quarantined, then classified.** You still get the instant "Okay, I'll remember that" — but behind it, each new fact is born owner-only and invisible to every model until a classifier clears it: with a **local model** configured (`memory.classifier_model`) the model judges *every* write — however a secret is phrased — with pattern-matching only as a fast lane for the obvious cases; without one, patterns are all there is: obvious secrets vault, ambiguous ones are held for review, and the People page banners the limitation (`memory.classifier_model` — a cloud model is *never* consulted about secrecy, and the classifier can even **split** a fact: the ledger keeps "I changed the wifi password yesterday" while the password itself lands in the lockbox). With no local model, ambiguous writes are held for review on the People page — a "held for review" badge with one-click Release / To-lockbox. The race window between speaking and classification over-protects instead of leaking.
- **A local classifier now covers consolidation too.** With a cloud brain, private-tier memories were withheld from the duplicate-merging pass entirely (the 4.0.2 privacy rule) — so restatements piled up. Now a local `memory.classifier_model` doubles as the consolidation model: the merge pass runs on it, private facts included, and nothing ever leaves the house to be judged. The People page tells you when this matters: memory on + no local model shows a banner (held-for-review facts pile up, private duplicates don't merge) with a one-click jump to the LLM service settings.
- **Memory capture is now a per-person choice.** Each person's page gained a capture mode: **Explicit** (the default — Kenzy only remembers what you ask, the promise unchanged), **Suggest** (she'll offer to remember useful facts — arrives with a later release), or **Auto** (she remembers durable personal facts on her own and always says so; her picks carry an "auto" tag, each one a Forget away, and they pass through the same quarantine pipeline as everything else).
- **Feature chips — the dashboard now tells you what's actually installed.** Each service's editor shows its optional features with honest states: active, available-but-not-enabled, or *enabled in config but NOT INSTALLED* — the state that used to be one log line (the Wyoming lesson). The **Install** button fills the missing dependency without moving any versions (your constraints honored) and restarts the service; system packages pip can't install show the copy-paste apt command instead of a fake button.
- **Services can be disabled (and enabled) from the dashboard.** Per-user systemd installs get a Disable button that stops a service *and* keeps it stopped (`systemctl --user disable --now` — restart policies can't resurrect it), plus Enable for services on the server's host. A stopped service on another host has nothing listening, so the page shows the one-liner to run there instead of pretending.
- **The Activity tab shows where the LLM's time actually went.** The latency waterfall's LLM segment is now subdivided into model calls vs tool calls vs service overhead, and clicking a row expands the ordered call list — each model call (named, so a fallback rescue is visible), each tool by name with its duration, and for fast-path rows, which intent handled it. Names and durations only; never call arguments or content, so secret exchanges keep their timings.
- **"What do you know about me?" now answers properly** — a spoken summary of what Kenzy holds for you (memories and lockbox count, with the most recent), and "what does the house know?" summarizes household-shared memory. Honest empties included.

### Fixed

- **The lockbox invariant now holds everywhere, not just in the store.** A pre-release adversarial review found four paths where a secret's value could still reach a model or a log, all closed: lockbox exchanges (store, spoken recall, forget) are never written into conversation history or short-term context (which feed future model calls — cloud included); the server and every service redact secret-shaped transcripts and lockbox replies from their INFO logs and the Activity tab (timing rows stay, content reads "[content withheld]"); spoken read-back and voice-forget now require every word of your topic to match the secret ("gym hours" can no longer read back — or delete — the gym locker code on a one-word overlap); auto-derived labels can never contain the value itself (a bare "swordfish" gets the opaque label "secret", and opaque labels never overwrite each other); a read-only dashboard (`controls: false`) can no longer export secret values or download a key-bearing backup; and the memory classifier's local-only rule now survives a failing primary — a cloud fallback model is never handed the text a cloud primary was denied.
- **Memories coalesce in seconds, not hours.** The mechanical dedupe pass is kicked after every classification (30s cooldown; the hourly run is now just the backstop), and two quarantine races are fixed: an identical quarantined twin can no longer dedupe away the released fact, and a fact released from quarantine re-enters the semantic pass's pending window instead of being stranded behind its high-water mark.
- **Dashboard confirmations work on phones now.** Every confirm was a native `window.confirm()`/`prompt()`, which some mobile browsers block silently — so Upgrade (and every other confirmed action) simply did nothing there. All of them are now in-page modal dialogs in the dashboard's own style, including the typed-confirmation variants (REMOVE), with Esc/backdrop cancel and keyboard focus handling.
- **Service logs show up in the Logs tab again.** The dashboard's service-log proxy still resolved targets from the static URL map — under mesh TLS (or with purely auto-registered services) every request failed quietly and the tab showed zero lines. It now resolves like every other proxy (static config ← auto-registration, TLS-aware), and the Services editor's "reachable" flag got the same fix.
- **The People page keeps up with memory.** Fresh facts settle shortly after the spoken acknowledgement (quarantine → classifier; instant for pattern-obvious cases, up to a minute when a cold local model judges), but the page never noticed: it now re-fetches memories and the lockbox every 15 seconds while open, and both the list and each person's page gained a ⟳ Refresh button. The lockbox Reveal panel also renders properly — value on its own line (the key/value answer), verbatim capture quoted beneath it, instead of everything mashed onto the label's line.

## [4.0.2]

### Added

- **Private memories no longer ride along to a cloud model.** The memory tiers have always controlled which *voices* hear a fact back — but a relevant private fact was still injected into the language model's context, and compared by the consolidation pass, using whatever model you configured. With a cloud brain, that meant the gate code went to the provider. Now **private-tier facts are withheld from cloud models entirely**: not in context, not in consolidation. Nothing you'd notice changes — private facts still answer instantly by voice (the fast path needs no model), and they consolidate normally once a local model is configured. Household-shared and about-them facts, visible to the whole house by design, still inject. If you'd rather have the old behavior, `memory.private_to_cloud: true` turns the protection off. (This is the first slice of the coming lockbox — full secret handling with encryption at rest arrives in a later release.)

### Fixed

- **The server can now be restarted from the dashboard without editing anything.** The Settings page only offered "Save & restart server," so a plain restart required a config change (or a terminal). A standalone **Restart server** button now sits beside it — confirm, restart, reconnect.

## [4.0.1]

### Added

- **The installer now asks where Kenzy's thinking should happen.** Server installs offer the choice up front — OpenAI (the quick-start default), Anthropic Claude, Ollama on your own hardware, or decide later — take the API key right there (hidden input, skippable), and write the choice into the dashboard-owned service config. Picking a non-OpenAI brain also offers the **fully local voice** (Kokoro) in the same breath, since the voice would otherwise still need an OpenAI key — making "everything in your house" a single installer path instead of a docs page. Scripted installs keep today's behavior (`--llm`, `--llm-model`, `--llm-url` flags for automation). Server-side: `ANTHROPIC_API_KEY` joins the central secrets the server distributes to the LLM service, so Claude works on multi-host deployments too.

## [4.0.0]

### Added

- **"What does Kenzy know about me?" now has a button — and so does "forget me completely" (v4 F7.4).** Every person's page gained a **Privacy & data** section with the three promises the memory era owes each household member. **Export** downloads one readable file: their person record, voice-profile info, and every remembered fact with tiers and timestamps. **Remove completely** is the guest-departure case — one typed-confirm action erases every fact they own, deletes their enrolled voice, and removes their record, atomically (memory is erased first; if the memory service is unreachable the action aborts rather than half-forgets — and household-shared facts they contributed deliberately stay with the house: the gate code doesn't leave with the guest). **"Don't remember them"** is a per-person opt-out: Kenzy keeps and reads *no* facts about them — no memory writes, no recall, no injected context, not even the short-term cross-room thread — while they remain a fully recognized voice for device control and questions; asking her to remember something gets an honest "memory is turned off for you at your request" — and ticking the box offers to erase what's already stored for them, too. No consent popups, no permissions matrix — seeing it and erasing it are now as easy as remembering it was.
- **Home Assistant's voice pipeline can now speak with Kenzy's voice — and hear with her ears (v4 F3.3/F3.4, Wyoming protocol).** kenzy-tts and kenzy-stt each grew an optional [Wyoming](https://github.com/rhasspy/wyoming) listener (`wyoming.enabled` in their configs; ports 10200/10300, the Piper/whisper conventions), so HA discovers them as native TTS and STT providers (*Add integration → Wyoming Protocol*, then pick them in your voice assistant's pipeline). The payoff: ask from the HA companion app and the whole turn is Kenzy end-to-end — her STT setup transcribes you (one whisper/cloud config for the house, with its fallback chain), her brain answers, and the reply comes back **in her actual voice** instead of whatever TTS the HA pipeline happened to have. Verified live: audio streamed into an HA pipeline came back as a correct spoken answer with every stage running through Kenzy. Both listeners reuse the services' existing synthesis/transcription paths (incoming audio is converted to the pipeline format when a client sends something else), run inside the existing service processes, and are off by default: Wyoming is plain unauthenticated TCP, so the listeners follow the service bind — loopback unless you've deliberately opened the service to the LAN. Requires `pip install wyoming` (now part of the `tts`/`stt` extras).
- **Link a household member to their Home Assistant login right from the People page — and Kenzy shows HA options only to households that actually use HA.** Each person's page gained a "Home Assistant person" control that links their HA app login, so Assist requests from their phone arrive *as them*: their memory, their personalized skills, their identity tier. When Kenzy can reach your HA it's a **dropdown of your real HA people** (no entity ids to type or typo — pick "Alex" and done); if HA is configured but momentarily unreachable it falls back to a text field (short forms like `alex` are auto-completed to `person.alex`). And the whole surface is self-gating: a household with no Home Assistant at all sees **no HA field and no HA tab** — nothing to configure away — while installing the kenzy-hass integration reveals them automatically (the server notices the first Assist request, even from an unmapped user). Disabling the `home_assistant` module in Skills collapses everything HA to a read-only note (an existing link is shown, never silently dropped). Mappings apply immediately (no restart), the People list tags everyone who's linked with "HA app", and clearing the field unlinks them (their Assist requests then get the safe unknown-visitor treatment).
- **Every skill now knows which front door you came in — and node-bound ones answer sensibly from your phone (v4 F3.2).** The Home Assistant Assist channel has no room speaker behind it, so skills that act *on* a speaker used to misfire from the app: "turn the volume up" targeted a node that didn't exist, "call the office" rang a room on behalf of nobody. Requests now carry the channel (`voice` from a room node — the default, so older servers are unaffected — or `assist` from the HA app), and node-bound skills refuse gracefully from a phone: volume, calibration, voice enrollment (which points you at the dashboard's People page instead), and intercom each explain they need a room device. Timers, alarms, reminders, and deferred commands do one better — since there's no "here" to ring in, a roomless "set a timer for 10 minutes" from the app makes Kenzy **ask which room** ("The office" as a follow-up works), while "remind me to stretch in 5 minutes in the office" schedules in one shot, fires on the office speaker, and carries your identity. The server also grew a backstop: a node-bound action reaching dispatch without an asking node is skipped and logged, never crashed — so a custom skill can't take the Assist lane down.
- **Kenzy understands when you've said the same thing twice — semantic memory consolidation, seconds after you speak.** "Remember that the dog is black" and later "remember that our dog has black fur" are one thought in two phrasings — now, moments after each "remember…", your configured model compares the new fact against its neighbors (same person, same privacy tier, never across either) and quietly merges restatements or supersedes corrected facts ("the plumber is Sam now" retires "the plumber is Joe"). It is deliberately incapable of destroying anything: the model can only *supersede* — originals leave recall instantly but stay on disk for the 30-day retention window, every decision is logged, malformed or overreaching model output degrades to keeping everything, and an unsure model is instructed to do nothing. Each write triggers the pass (rate-limited to one run per 30 seconds, so dictating five facts costs one model call, not five); failures retry in 15 minutes; a daily sweep catches anything left over — all visible with per-run history at `GET /jobs`. Facts with nothing similar on file skip the model entirely, so the common case costs nothing.
- **Memory tidies itself — and Kenzy grew a background-job runner (v4 F5.5 thin + F2.7 mechanical).** Saying "remember that…" twice no longer leaves two copies: a periodic maintenance job dedupes exact repeats (spelling and punctuation folded — "the Wi-Fi code" and "the wifi code" are the same fact, newest wins), removes expired facts, and clears superseded tombstones past their retention window (30 days) — all pure bookkeeping, **no AI model involved**, and every removal individually logged. It runs hourly by default (`memory.maintenance_interval`, 0 disables). Under it sits the new shared job runner — one sequential, error-isolated loop per service that all future periodic work registers with (the rule: no feature spawns its own timer loop), visible at the token-gated `GET /jobs` with per-run history. Smarter consolidation (merging related facts into summaries, on the model *you* configure) comes with the memory hardening phase.
- **Kenzy remembers — per-person memory, gated by voice (v4 F2, phase 1).** "Remember that the gate code is 4312" → she keeps it; "what do you know about the gate code?" → she answers — instantly on the fast path for common phrasings, and through the model (which now holds `remember`/`recall`/`forget`/`share_memory`/`make_memory_private` tools) for everything else. Facts belong to the **person** Kenzy recognized (the People tab), not a name string, and live in three tiers: **private** (only you can hear it back), **personal-public** (facts about you others may ask), and **shared** (household facts — "everyone should know…" puts it there, or "share that with the house" promotes it later). Relevant facts are quietly injected into the model's context each turn — scoped to the asker — alongside a new hours-scale per-person thread of recent exchanges that follows you across rooms (the 3-minute per-room history remains for turn-by-turn flow). **An unrecognized voice gets none of this**: no writes, no reads, no injected context — and, closing a hole live-testing found, no *echoes* either: an answer built from private memory is tagged to its owner in the room history, so a stranger asking seconds later can't get the fact replayed. Storage is a plain, human-readable JSONL ledger in the config home's `data/` tree (deliberately not a database — tolerant per-record versioning means version-skipping upgrades never need migrations; it rides backups and multi-host restore automatically), behind token-gated `/memory` endpoints on kenzy-llm. Writes are explicit in this phase — Kenzy only remembers what you ask her to. And you can see it all **where it belongs — under People**: each person's page lists everything Kenzy remembers for them (with tier, age, and a Forget button), the People list shows a memory count per member, and the page-level sections cover what belongs to no one person — household-shared facts, a search across every fact, and orphaned facts from deleted people (the dashboard is a credentialed admin surface, so it sees all tiers; editing/re-tiering and retention windows come with the hardening phase).
- **A People tab in the dashboard — everything voice-identity in one place (it replaces the Speakers tab).** The identity foundation shipped as a config file; now there's a friendly editor for it, and rather than adding another tab beside Speakers, the two became one screen with one model: **enrollment is person-first**. A person can exist without a voice, but every enrolled voice belongs to a person — so the page is just people. Add a household member, click **Enroll voice** on their card, pick the room to record from, and Kenzy walks them through the sentences; the voice profile is stored under the person's stable id and linked automatically, so there's no separate "voice name" to invent and no linking step to forget. Enrolling again later adds more samples to the same profile — different days, distances, and microphones make recognition more reliable. The "Hey Kenzy, enroll me as Alice" voice command follows the same rule: it finds or creates the person record for the name it hears, so no path can produce an ownerless voice. Voices from an older setup (or the `kenzy-enroll` CLI) appear in a *Voices without a person* section — one click assigns each to a person — and deleting a voice or a person keeps the records consistent automatically (renaming a person never touches their voice profile, since it's keyed by their id). Changes apply immediately — the running pipeline resolves the new mapping on the next request, no restart. Hand-set Home Assistant / phone links in the file are preserved untouched. Needs `dashboard.controls: true` (otherwise the tab is read-only).
- **Skills can now require a recognized voice (identity tiers enforced as a contract).** Building on the identity foundation, a skill or fast intent can declare `min_tier="recognized"` — and an unrecognized voice simply doesn't get it: the tool is withheld from the model entirely (it can't be talked into calling it), a direct call is refused with a spoken explanation, and gated fast intents never run. Skills without a gate stay available to everyone, which is the right default for device control and general Q&A. Nothing built-in is gated yet — this is the enforcement point the coming per-person memory arrives behind, and custom-skill authors can use it today (the dashboard's Skills tab badges gated entries). Fail-closed by design: no tier information means `unknown`.
- **Identity foundation: Kenzy can now tell *who* is speaking, not just a name (v4 groundwork).** A new person layer maps voiceprints to a household member and carries a **confidence tier** through the pipeline — so a request now arrives as `(person, confidence, room)` rather than a bare speaker string. An optional `data/people.yaml` links one or more enrolled voiceprints to a person (with optional Home Assistant user / phone for later channels); with no file, behavior is exactly as before (the raw speaker name passes straight through). This is the substrate the coming per-person memory and privacy features build on. Fully standalone — voiceprint-only households work, HA links are optional.

### Fixed

- **Voice enrollment no longer dies mid-flow on a slow machine — and a missed prompt now retries instead of stranding the session.** Two field findings from live testing. First, the session's timeout was a fixed 120-second total budget — a real five-prompt run through local (Kokoro) TTS legitimately takes longer, and the session was cancelled while the person was still dutifully reading prompts. The timeout now measures **inactivity** instead (60 seconds per prompt, reset every time a sample arrives): an enrollment that's making progress runs as long as it needs, while one that's been walked away from is abandoned twice as fast as before. Second, if the person **hesitated past the ~8-second listening window**, the node's window-expired signal never reached the enrollment loop — Kenzy just went silent and eventually announced a timeout. An expired window now counts as a missed sample: "I didn't catch that. Please say: …" and a fresh listening window, like any unclear capture.
- **The Speakers, Skills, and Home Assistant dashboard tabs work again when TLS is on.** Since TLS became the install default, those tabs could show "service not reachable" even with the service healthy: the dashboard proxied to the backend using the *static* URL from `server.yaml` (still `http://…`) instead of the `https://` address the service actually registered itself under. The proxy now uses the same resolved address the health checks do, so it follows the service's real scheme. Plaintext setups are unaffected.

## [3.12.0]

### Added

- **Restore a backup from the dashboard, not just the command line.** Settings now has a **Restore from a backup…** panel next to Download: pick a backup file, type `RESTORE` to confirm, and it's uploaded into the server and applied — configs, curation, voice profiles, schedules, and your custom skills — after which the server restarts and the rest of the fleet re-pulls and self-populates automatically. So a full recovery is now a browser round-trip. It's a deliberate overwrite of live settings (hence the typed confirm), and because a backup can contain custom skills (executable code you wrote), the upload runs under your admin session — the same trust the dashboard's upgrade button already carries. Very large archives that include `models/` still use `kenzy-init --restore`.
- **Your API keys can live on one host now — the server hands them to the services that need them.** Building on 3.11's signed service channel, the server serves each backend service the secrets it needs (the STT/TTS/LLM services get your OpenAI key, the LLM service also gets the Home Assistant key, the speaker service its HuggingFace token) over the authenticated, encrypted config channel — so you set a key once, on the server, instead of copying `.env` files to every machine. Secrets ride **only** a token-proof request over TLS (never plaintext, never an unauthenticated one), are never written to disk on the receiving host, and the server's value wins over any stale local copy, so rotating a key centrally actually takes effect everywhere. The dashboard's **Settings → API keys** is now genuinely fleet-wide.

### Changed

- **The shared token no longer travels the network at all.** 3.11 introduced signature-based service auth but still sent the raw token alongside for compatibility; 3.12 stops sending it — services and the server prove they hold the token by signature only, and **nodes now prove their join the same way** (the join token no longer rides the `hello` handshake). Combined, the fleet's shared secret is never transmitted, which is the precondition that makes central secret-serving safe. Old releases are still *accepted* for a straggler, but nothing sends the raw token any more.

  !!! Upgrade note
      Upgrade the **server first** (the dashboard's upgrade-all already does this). A node or service upgraded ahead of its server briefly can't authenticate to the older server and will retry until the server catches up — harmless, but upgrade in order to avoid it.

## [3.11.0]

### Added

- **A node can boot from three environment variables — no `node.yaml` needed.** Point `KENZY_SERVER_URL` at the server, set `KENZY_SERVER_TOKEN` (if the server requires a join token) and `KENZY_NODE_ID`, and a room device starts with no config file — everything operational is pulled from the server as always. `KENZY_NODE_ID`, when set, is authoritative and never written to disk, which is exactly how two node instances run on one machine (two speakerphones, two units, two ids). A systemd unit with a few `Environment=` lines is now a complete node install. (The token env var was renamed `KENZY_SERVICE_TOKEN` → `KENZY_SERVER_TOKEN` to match; the old name is still accepted everywhere.)
- **A rebuilt host repopulates its own data from the server — restore just got real.** A backup has always bundled the whole fleet's state (enrolled voice profiles from the speaker host, skills and Home Assistant curation from the LLM host), but *restoring* only ever unpacked onto the server — so on a multi-host setup, the other machines came back empty. Now a service that boots with an empty data directory fetches its slice from the server over the same signed channel it already uses for config, the same way it pulls its settings. Disaster recovery collapses to "restore the server, turn the hosts back on" — nobody hand-copies `.npy` files. It's self-healing beyond restore, too: reimage a speaker box and it comes back with its enrolled voices automatically. **Local data always wins** — a host that already has its data never calls the server, so a stale copy can never overwrite a live one. (Co-located installs are unaffected: everything already shares one config home.)

### Fixed

- **Restoring a backup keeps TLS on, and a backup never carries your private key.** Two related fixes now that installs can have a TLS certificate: a backup deliberately excludes the certificate/key (private-key material, like `.env`, never enters an archive — wherever it sits), and when the restored `server.yaml` expects a certificate that isn't there, Kenzy mints a fresh self-signed one — and if the backup was restored into a **different folder** (a new machine), it relocates the certificate under the new config home and rewrites the `tls:` block, so a cross-machine restore keeps TLS on. Without this, a restored server would find its cert missing and quietly fall back to plaintext; now a restore preserves the encrypted posture, and — because Kenzy's clients don't pin certificates — a freshly-minted cert is a non-event.

### Changed

- **Service-to-service auth no longer puts the shared token on the wire, and TLS is the install default.** The backend services and the server now prove they hold the fleet token by *signing* each request (HMAC) instead of sending it — so an eavesdropper on the LAN, even one that terminates TLS, learns nothing it can replay, and an impostor can't answer for the server. The config channel goes further: the server signs its reply bound to its own TLS certificate, so a relay presenting a *different* certificate is caught when the service checks the answer against the cert it actually saw. Fresh installs now enable TLS by default (encrypting node audio, the dashboard, and the service mesh) — pass `--no-tls` for the old plaintext behavior. This is groundwork: once a later release drops the transitional token header, the server can safely serve API keys to the fleet, so secrets live on one host instead of every host. Mixed-version fleets upgrade seamlessly (the pre-3.11 token header is still accepted for one release); existing plaintext installs are untouched until you enable TLS yourself.

## [3.10.2]

### Fixed

- **Singular means THE device; plural means the group.** "Turn on the light" was still taking the room's curated default set (the lamps) instead of the fixture named "Office Light." The rule is now grammatical: a singular group word ("the light", "the fan") resolves to the device named "\<Room\> Light"/"Light", else the room's only one of that type — and when no specific referent exists (several lights, none named "Light"), it degrades gracefully to the plural action, since the curated default set is your answer to "what does the lighting mean in this room." Plural ("the lights") and "all/every" phrasings behave as before, with "all" always sweeping the full set. Verified live across the whole matrix.

## [3.10.1]

### Fixed

- **"The office light" means the fixture named "Office Light" — not every light in the office.** Singular device references that exactly match a real device name now hit that device; the generic forms ("the light", "the lights") keep their room-group semantics with your curated defaults. Works with the room words spoken or not ("turn on the office light" from any room; "turn on the light" standing in a room whose fixture is named just "Light").
- **A garbled word can no longer actuate a random device.** Field incident: "turn off the hall light" was misheard by speech-to-text as "turn off the *hot* light" — and the fuzzy matcher confidently turned off a back-porch fixture, because the word "light" alone scored a dozen unrelated devices past the match cutoff. Device matching now enforces **token coverage**: every word you said must be accounted for in the device's name, so tolerance stays *within* words (prefixes, plurals, near-misses like "hall"→"Hallway" still match) while an unexplained word disqualifies the candidate. Garbled phrases fall through to the AI tier, which sees your room and the device map and can clarify or make a room-scoped judgment out loud — instead of the matcher firing silently on the wrong thing. The same gate fixes a perfect-transcript bug it uncovered: "hall light" used to *lose* to any device literally named "Light."

## [3.10.0]

### Added

- **TLS now covers the service mesh too.** With `tls: {cert, key}` set on the server, the backend services (STT, TTS, LLM, speaker) no longer stay plaintext: the server hands its cert pair to co-located services through the config they already pull, each service's own listener comes up as **https**, the auto-registration heartbeat reports the scheme, and every internal call (pipeline, dashboard proxies, health checks) speaks https — unverified by default, like every other Kenzy client (`KENZY_TLS_VERIFY`/`KENZY_TLS_CA` to opt into real verification). Remote hosts in a multi-host fleet supply their own pair via `KENZY_TLS_CERT`/`KENZY_TLS_KEY`. Zero extra setup for the common case: enable TLS on the server and the whole stack — node audio, dashboard, and now the mesh — is encrypted. Missing cert files degrade to plaintext with a warning, never a boot failure.

### Fixed

- **The dashboard's startup log now says `https://` when TLS is on** (it always said `http://`, which sent you to a URL the browser couldn't open).
- **"Mute the TV." now works with the period.** Speech-to-text transcripts arrive capitalized and punctuated, and the media/vacuum command patterns (fixed-word forms like "mute the TV", "skip this song") hard-missed on the trailing period — falling through to the LLM, which in one observed case muted TVs house-wide instead of the one in the room. The intent parser now reads a normalized copy of the transcript; the device-name patterns were already tolerant, and now everything is.
- **Two device-resolution bugs found by talking to Kenzy — literally.** A new automated voice-test rig (synthesized speech through a real speaker into a real node mic) caught both on its first run. First: plural device phrases ("turn on the office lamps") compared the spoken stem against Home Assistant's Title-Case names case-sensitively, so the match never fired and the request fell to the slower LLM tier. Second: devices named after their own room ("Office Lamp" in the office) were unreachable by their natural phrasing — "turn on the office lamp" stripped the room word and the leftover missed the fuzzy cutoff. Both now resolve instantly; the room-name retry deliberately uses strict whole-string matching so the room word alone can never pull the command onto the wrong device ("office ceiling fan" defers to the LLM rather than guessing a lamp).

## [3.9.0]

### Added

- **Optional TLS — encrypt everything Kenzy says on your network with one config block.** Point `tls: {cert, key}` in `server.yaml` at a certificate pair and the node WebSocket port speaks **wss** and the dashboard serves **https** (its login cookie gains the `Secure` flag). A **self-signed cert is all you need** — the docs have the one-line `openssl` command — because Kenzy's own clients are built for the home-LAN reality: nodes and backend services connect *encrypted but unverified* by default, so live mic audio, transcripts, and config stop being readable off the Wi-Fi without installing a CA on every device. mDNS advertises the TLS flag, so auto-discovering nodes and services switch to `wss://` on their own; only a hand-set `server_url` needs updating. Verification is there when you want it: `tls_verify: true` / `tls_ca:` in `node.yaml`, or `KENZY_TLS_VERIFY=1` / `KENZY_TLS_CA` for the services. Plaintext remains the default — TLS is a deliberate opt-in, never required, and the reverse-proxy path still works too.
- **Scenes, scripts, buttons, and toggle helpers are voice-controllable.** "Hey Kenzy, activate movie night" / "run the goodnight routine" / "press the coffee maker button" / "turn on the sprinklers" — four new Home Assistant domains (`scene`, `script`, `button`+`input_button`, `input_boolean`), all on the instant no-LLM fast path. They resolve **by name across the whole house** (scenes and scripts usually have no room, so room scoping doesn't apply), the whole verb family works (activate/run/start/execute/launch/press/push), and a trailing qualifier is understood ("the movie night *scene*" matches a scene named just "Movie Night"). Sanity filters are built in: diagnostic device buttons (Identify/Restart/Update) are dropped automatically, and **Kenzy's own HA entities** (the MQTT bridge's trigger/stop/mute per node) are never voice targets — previously "turn on the lights" could literally flip the room's Kenzy mute switch. Heads-up: activating a scene or script is not speaker-gated — exclude sensitive ones via curation.
- **The robot vacuum takes voice orders.** "Start the vacuum," "stop the vacuum," "send it home" / "back to the dock" — the `vacuum` domain joins voice control, all on the instant fast path. "The vacuum" needs no name: Kenzy resolves it positionally (the asking room's vacuum, else the house's only one — several, and she asks which). "Turn the vacuum on/off" is understood as start/stop, and named vacuums ("run Rosie") work from any room.
- **Media players answer transport commands.** "Pause," "resume," "skip this song," "turn the TV up," "mute the television," "pause the music in the living room" — the `media_player` domain joins voice control with the full transport verb set (play/pause/next/previous/media volume/mute/on/off), all instant. Targeting is smart about *where the music actually is*: your room's player first; a room with several picks the one playing; and "pause the music" from a room with no player stops the one thing playing anywhere in the house. Volume words that name a media thing go to the player and move it **three device notches per command** (`media_volume_steps` to taste — one notch per sentence is nobody's idea of control); the bare forms ("turn it up," "mute") still control Kenzy's own speaker. Starting *new* music by name ("play some jazz") is deliberately not included — that's the Music Assistant integration, coming separately; Kenzy says so instead of guessing.
- **The Home Assistant tab got easier to live with.** Rooms are now collapsible accordions — collapsed by default, with a device count on each header — so a big house doesn't mean a wall of entity rows. And Kenzy's own HA entities (the per-node mute switch and trigger/stop buttons from the MQTT bridge) now render as plainly inert: they're excluded from voice control at the code level, so the tab stops offering curation controls that couldn't have applied.
- **The device fast path got sharper ears.** Two long-standing resolution bugs fixed while building the above: device-name matching was case-sensitive (your lowercase words vs HA's Title Case names cost ~18 similarity points — "movie night" missed "Movie Night"), and commands without an article never matched ("turn on guest mode" failed where "turn on *the* guest mode" worked). Both now resolve instantly instead of falling through to the LLM.
- **The installer asks the TLS question for you.** A server or all-in-one install now offers to enable TLS (default: no — plaintext stays the default posture): say yes and it generates the self-signed pair into your config home, writes the `tls:` block into `server.yaml`, and hands you an `https://` dashboard URL. Scriptable too: `--tls` / `--no-tls` skip the question, and `--tls-cert` / `--tls-key` bring your own certificate.

## [3.8.2]

### Added

- **The stop words got a full vocabulary — and the docs finally teach the basics.** Kenzy has always ended a session silently, before any AI involvement, when the whole utterance was a stop phrase ("stop", "be quiet", "shut up"…). That set now covers the rest of the family — "stop it", "stop talking", "hush", "shush", "enough", "that's enough" — all instant and reply-free (you asked for quiet). The polite bail-outs ("never mind", "forget it", and now "cancel") keep their brief spoken "Okay." And there's a new **[Talking to Kenzy](https://docs.kenzy.ai/talking-to-kenzy/)** docs page — waking her, interrupting mid-sentence, the stop words vs. the bail-outs vs. muting, how conversations work without repeating the wake word, and what every chime means. The five-minute guide to hand a houseguest.

## [3.8.1]

### Fixed

- **The "Upgrade services + nodes" button now retires itself when the work is done.** It stayed visible even after every service and node was on the latest version. It now watches the live fleet — running versions vs. the latest release, plus any service upgraded on disk but not yet restarted — and disappears once everything is current, exactly like the server's own upgrade button. (It still appears, disabled, while the server itself is behind — preserving the two-step order — and unknown/unreported versions keep it visible, since they can't be vouched for.)

## [3.8.0]

### Fixed

- **Changing the dashboard password now survives upgrades.** Both `kenzy-passwd` and the dashboard's change-password form wrote the new login into `server.yaml` — which, on a `kenzy-deploy` host, the next upgrade's config sync overwrites with the operator tree's copy, **silently reverting the login** (typically back to the default). The auth block now lands in the `server.local.yaml` override layer instead: protected from deploy syncs, redirected out of site-packages on packaged-default installs, and merged over server.yaml at boot. A password already set in `server.yaml` keeps working; the next change simply migrates it to the safe layer.
- **Services now report the version they're actually running.** The version shown in the dashboard was read from the installed package on disk each time — so the moment pip upgraded the shared venv, every still-running service claimed the new version without executing a line of it (this hid a stale kenzy-llm during a bug hunt: it "was" upgraded, but wasn't). The running version is now captured at process start, and services additionally report the on-disk version — when the two differ, the fleet's service chip shows a **"restart" badge** (hover for detail) and the Settings page notes "vX.Y.Z installed — restart to apply" for the server itself. Upgrade visibility now tells the truth: what runs, what's on disk, and whether a restart is still owed.
- **She no longer says the room twice.** Home Assistant devices often carry their area in the name ("Master Bedroom Light" in the Master Bedroom), and the spoken confirmation rendered room + device — "I turned off the master bedroom master bedroom light." When the device name already starts with the room name, the name now stands alone: "Turned off the master bedroom light." (Spoken phrasing only — resolution still matches the full device name, so saying "master bedroom light" works exactly as before.)

### Added

- **A house-wide chime for automations — `kenzy/chime`.** Born from a real doorbell: an HA automation can now make every Kenzy node (or just some rooms) play a chime **instantly** — no speech synthesis, no TTS dependency, just a ding-dong everywhere the moment the button is pressed. The payload picks the sound and can loop it: `{"sound": "doorbell.wav", "seconds": 8, "rooms": ["kitchen"]}` (bare string = a sound name once; empty = the new bundled doorbell). Loops repeat in whole rings — never cut off mid-"dong" — and cap at 30 seconds so a buggy automation can't ring the house forever. Sounds are the bundled ones or names you map in `integrations.mqtt.chimes` (name → WAV on the server host) — automations never get to point at arbitrary files. And chimes are **alert audio**: a muted node still plays them at the same low floor as the wake-word chime, because "someone's at your door" is exactly what mute shouldn't hide. (Nodes on older releases just honor mute.) The docs' doorbell recipe now uses it.

- **Lists can be deleted by voice now — carefully.** "Delete the grocery list" asks before it acts: "The Grocery list still has 3 items on it. Delete it for good?" — and only the spoken *yes* that follows deletes anything. Because deletion is destructive and voice mishears, the rules are deliberately stricter than for everything else: the list name must match **exactly** (or a configured alias) — no fuzzy matching, and a bare "delete the list" never falls back to the default list, it asks which one. Only locally-stored lists can be deleted; a list synced from an outside service (Google Tasks, Todoist…) gets a spoken explanation instead. The confirmation is room-scoped and expires, so someone else's "yes" in another room — or yours a minute later — deletes nothing. ("Delete milk *from* the list" still removes just the milk, as before.)

- **Calibration is one guided, spoken-or-watched flow — and it now detects echo cancellation by itself.** All calibration runs as a single server-driven session with two entry points. Say **"Hey Kenzy, calibrate"** and she walks you through it out loud; or click **Set up / calibrate audio** in the dashboard, where the same session runs with the instructions on screen (and a beep instead of speech — no TTS needed, works fully local). Either way: a few quiet seconds, then the wake word four times — the repetitions double as a sample of *your voice at your normal distance*, and the silence threshold anchors to that voice (derated for distance) instead of the room's momentary noise floor, so a washing machine starting up later can't invalidate it. Phases have quality gates (a door slam restarts the quiet phase; too little heard extends the window), values apply automatically, and a final **live verify** waits for a real wake — nudging the threshold down (bounded, twice) if she doesn't hear you. You get an honest **noise-to-speech separation verdict**, and anything that can't be measured cleanly is left unchanged and said so. The dashboard can also watch a voice-initiated calibration live, and the headless `kenzy-node --calibrate` shares the same math.

- **She notices whether her speaker cancels its own echo — and sets `hardware_aec` for you.** The flag that decides whether you can interrupt Kenzy mid-sentence (and whether intercom/alarms work on a node) had to be set by hand, by someone who knew it existed. Now calibration measures it: at the start of the flow she plays a known signal through her own speaker (the spoken intro, or the beep) and reads how much of it leaks back into the microphone. Near-silence means real echo cancellation; hearing herself loudly means none. The verdict is applied **immediately** — the rest of calibration and everything after runs under the correct conversation mode — and announced in plain consequences: *"I can hear you even while I'm talking, so feel free to interrupt me"* vs. *"wait for me to finish speaking before you reply."* Ambiguous readings change nothing, and the probe is skipped when the node is muted or very quiet (a silent speaker would look like perfect cancellation).

- **Upgrading the whole system is two clicks now, with a running log.** The Settings page gains an **"Upgrade services + nodes"** button next to the existing server upgrade — together they replace clicking Upgrade on every component (a 9-click affair on a full house). The buttons enforce the logical progression: step 2 sits disabled (with a tooltip) until the server itself is current, arming automatically once step 1's restart completes. Step 1 upgrades the server (as before); step 2 walks every backend service and then every connected node **one at a time**, streaming a live per-item log ("[2/6] tts — installing…", green/red per result) so you can watch it work. Sequential on purpose: co-located services share one venv, and parallel pip runs into it don't end well. Smarter, too — **every upgrade path (including the existing buttons) now checks first whether the target version is already installed**: if it is, the component is simply restarted ("v3.7.4 already installed — restarted to apply it"), and if it's already *running* the target, nothing happens at all. After a server upgrade on a single-box install, step 2 typically completes in seconds — one real install, the rest restarts.

### Changed

- **All three config editors now speak one visual language** (previously only the node editor worked this way; the Services and server Settings editors showed every field pre-filled with its effective value plus "override"/"inherits" labels to explain where it came from). A field is **filled only with what you set**; the greyed placeholder always shows what applies when you leave it alone (the packaged default, the server.yaml value, or the code default); on/off and choice fields get an explicit `inherit (…)` option showing the inherited value; and a set field's row is highlighted. The redundant "override" / "inherits …" text labels are gone everywhere. New with this: **clearing a field is now how you revert it** — emptying an input (or picking `inherit`) removes the key from the override file on save, falling back to the default, and removing the last key deletes the override file entirely. Previously a Services value, once saved, could only be reverted by editing YAML on the server.

## [3.7.3]

### Fixed

- **The letter-by-letter grocery list, actually fixed this time.** 3.7.1's registry guard assumed the model was deviating from the tool schema — but the schema itself was wrong: parameters annotated with modern union syntax (`items: list[str] | None`) fell through the schema generator's `typing.Union` check (PEP 604 unions have a different runtime origin, `types.UnionType`) and were advertised to the model as plain strings. So the model was *instructed* to pass `"broccoli"` as a string, the guard saw a string-typed parameter and correctly left it alone, and `create_list` exploded it into eight one-letter items. Union-syntax annotations now generate the same schema as `Optional[...]` always did — `create_list.items` correctly advertises as an array (the only list-typed parameter affected), and the 3.7.1 guard now engages as designed for any model that still sends a bare string.
- **Dashboard-edited server settings no longer vanish on `kenzy-deploy upgrade`.** Settings saved from the dashboard's Settings tab (backend URLs, MQTT integration, dialog/alarm tuning…) live in `configs/server.local.yaml` on the server host — but unlike the per-node and per-service override stores, that file wasn't protected from deploy's config syncs, so every upgrade deleted it and silently reverted those settings. It's now excluded from the overwriting rsyncs in both source and pypi modes, same as the rest of the dashboard-owned state. (Settings survive restarts either way; it was specifically upgrades that wiped them.)

## [3.7.2]

### Fixed

- **The `experimental` flag is truly off by default again.** The 3.7.1 package accidentally shipped a `server.local.yaml` containing `experimental: true` inside the bundled default configs (a dashboard toggle on a dev machine wrote its override next to the packaged `server.yaml`, and the file got committed). Impact was narrow — only a server running with *no config home at all* (bare `pip install`, no `kenzy-init`) picked it up, and the flag currently only recolors the dashboard favicon — but it's gone from the package now. The underlying hole is also closed: the server's override file is never read from or written to the packaged data directory — a dashboard edit on a packaged-default server now lands in the config home (`$KENZY_HOME`/`~/.config/kenzy`), same as every other redirected write.

## [3.7.1]

### Changed

- **The "Kenzy" wordmark in the dashboard sidebar is now a home link** — clicking it returns you to the Fleet page, the way a top-left logo conventionally does.

### Added

- **The dashboard has a favicon** — a small Kenzy mark (gold "K" on the petrol brand square), so the browser tab is no longer blank. Served locally, so it works offline like the rest of the dashboard.
- **An `experimental` flag in `server.yaml`** — set it `true` to opt a server into features that aren't ready to ship officially (nothing is gated by it yet; it's the switch future preview features will hang off). It's editable from the dashboard's Settings tab, and it has one immediate, visible effect: the dashboard favicon switches to the experimental mark — colors swapped (petrol "K" on a gold tile) with a corner badge dot — so an experimental instance's browser tab is tellable from production at a glance.
- **Kenzy nodes sort themselves into Home Assistant areas.** The MQTT integration now sends each node's room name as the device's *suggested area*, so a node lands in the matching HA area automatically (HA creates the area if it doesn't exist — works best when your Kenzy room names match your HA area names). It's only a suggestion: HA applies it while the device has no area, and an area you've assigned by hand is never touched. Existing devices without an area pick theirs up on the next bridge reconnect or server restart.

### Fixed

- **"Add broccoli to the grocery list" no longer adds b, r, o, c, c, o, l, i.** When the model called a list-taking skill with a bare string instead of an array (a schema deviation models occasionally make), the string was iterated per character — so a single item became one entry per letter. Tool arguments are now repaired against the skill's schema before the call: a string sent for an array parameter is wrapped into a one-item list. Fixed at the registry level, so every list-taking skill (lists, schedules, announcements) is covered, not just the one that got caught.
- **The Skills tab now works on phones.** Two mobile issues: (1) the whole tab was stuck in a desktop-width horizontal scroll — the dashboard's content sections are laid out in a grid, and grid items don't shrink below their content by default, so the truncated skill descriptions forced the page wider than a phone screen; content sections can now shrink to fit. (2) When a skill group was expanded, long function names like `handle_home_control` overlapped the call-count/toggle box; on phones the row now stacks — the name gets the full width, with the count and toggle beneath it. Desktop layout is unchanged.

## [3.7.0]

### Added

- **Talk over Kenzy and she yields — real full-duplex turn-taking.** When Kenzy asks you something and holds the floor for your answer, you no longer have to wait for her to finish (or say the wake word) before replying. She now listens *while she's speaking*, and the instant you start answering, her voice ducks — that quick volume drop is the "go ahead" — and once your speech is confirmed (~300 ms) she stops and it's your turn, with a pre-roll buffer so nothing you said during the duck is lost. A false trigger — a clink, the dog — just un-ducks and she finishes her sentence, so a stray noise costs a half-second dip, never a truncated reply. This happens **only inside a dialog she started** (an expecting-a-reply moment); outside a dialog the wake word remains the only way in, keeping "when is the mic open" legible. Requires an echo-cancelling speakerphone (it's listening over her own voice) — on a `hardware_aec: false` room the feature is simply inert and the 3.6.0 strict-turn behavior applies.



### Changed

- **The config editors are far easier to use.** Settings across the node config, the backend Services editors, and the server Settings tab are now **grouped into logical sections** (node: Audio, Wake word, Capture/VAD, Dialog, Sounds…; speaker: Model / Identification / Enroll; server: Backend services, Dialog & alarms, Discovery, Integrations; plus each backend's provider blocks) instead of one long alphabetical list, each field carries a **one-line description**, and **irrelevant fields hide themselves**: pick `whisper` and the OpenAI settings disappear (and vice-versa), pick a TTS provider and only its options show, turn VAD off and the VAD-only timings vanish. Booleans stay on/off choosers, with a "default" option where a value can be unset — consistent across every editor. Each editor also carries a **"Docs ↗" link** to the full reference, and the field descriptions were rewritten to drop jargon (loudness/voice-detection instead of RMS/VAD) and state ranges.
- **A few behaviors that were hardcoded are now configurable.** The thermostat comfort clamp for "make it warmer/cooler" (`skills.home_assistant.thermo_min`/`thermo_max`, default 65–85°F), how persistently a firing alarm re-rings (`alarm.ring_repeats`/`ring_interval`, default 10× / 25 s), and how many follow-up turns a multi-turn dialog holds the floor for (`dialog.max_turns`, default 6) can all be set from config / the dashboard now.

### Fixed

- **`unknown_speaker` is no longer duplicated across two configs.** The name for an unidentified speaker was editable in *both* the server settings and the speaker service settings — and the server actually compared the service's result against its *own* copy, so setting them differently would silently break speaker handling. It now lives in one place (the speaker service config); the server reads the same value the service uses. One setting, one source of truth.
- **She holds the floor more reliably.** Whether Kenzy keeps the mic open for your reply is a flag in her structured response, and the model dropped it intermittently — so a knock-knock joke would sometimes end on the setup line, or the last of several requested questions wouldn't wait. Fixed at the output mechanism: the reply is now requested as a **strict JSON schema** where the floor-hold flag is a required field, so the model has to decide it every turn instead of silently omitting it. Providers that don't support structured outputs fall back to the previous prompt-based behavior automatically. (This also let us delete the old text-matching heuristic that force-closed replies containing phrases like "anything else?" — the model's schema-required flag now decides, so no reply-parsing is needed.)

## [3.6.1]

### Added

- **The LLM `base_url` is editable from the dashboard now.** It's the endpoint you point at a local model (Ollama, LM Studio) or a proxy — and the security design always treated it as dashboard-editable (which is why your OpenAI key never travels to it) — but it shipped commented out, so it never actually appeared in the Services → llm editor. Now a real (empty) key, so you can set it without hand-editing a file. Consistency pass on the node sound settings too: every sound with a bundled default shows that default in the editor instead of a bare "default".
- **Intercom now rings on the caller's end.** After "Ok, calling the kitchen," there used to be dead silence while the other room was rung and asked to accept — you only knew the call had connected if the other person happened to speak. The caller now hears a **ringback loop** (`sound_ringback`, a `ringback.wav` you can customize per node) during that wait, which stops the moment the call connects (the connect chime takes over), is declined, or times out. Fills the one silent gap in the call-setup flow.

### Fixed

- **Weather spoken output is less awkward.** Temperatures no longer come out as "71 degrees F" (the TTS engine reading the "F" in "°F" as a letter) — now "71 degrees" — and wind is spoken as "miles per hour" rather than "m-p-h". The National Weather Service's terse headline phrasing ("Chance Showers And Thunderstorms") gets a light polish toward a spoken sentence. Still somewhat clipped in places — the source data is terse by design and fully smoothing it deterministically isn't worth the fragility.

### Changed

- **The Activity tab's latency bars now share one time scale.** Each interaction's waterfall used to be drawn relative to *its own* total, so a 2-second LLM call and a 1-second one could look identical — you couldn't compare runs by eye. Bars now map to absolute milliseconds against a single axis across all visible runs (shown as "full width = Xs", with quarter-scale reference marks), so a slow LLM run is visibly wider than a fast one and fast-path replies read as slivers next to LLM ones.

## [3.6.0]

### Changed

- **Dialogs stopped beeping at you.** Multi-turn conversations ("tell me a knock-knock joke") used to run wait-your-turn ceremony on every exchange: her line, *ding*, your line, *hold music*, her line, *ding*… The whole mid-dialog soundtrack is gone. Kenzy's question is the cue — when she stops talking in a dialog, she's listening, every time, silently. No chime between turns, no waiting sound between turns, and a dialog that ends with a spoken reply ends **silently** (the reply is the closure). The end cue now means exactly one thing — "I stopped waiting" — and plays only when you leave a held dialog unanswered (~8 seconds, tunable via `dialog_no_speech_timeout_ms`). The wake beep is untouched: it marks the privacy boundary and stays. Voice enrollment keeps its record-after-the-tone chime (the protocol's new `expect_utterance` `cue` flag decides per flow); intercom consent answers now open silently too.
- **A clink can't start (or cut off) a dialog turn.** Follow-up capture used to open hot with raw energy detection — a cough or dish could trip it, and then "silence" would end the turn on garbage. Turns now start on **sustained, speech-classified audio** (~300 ms, Silero-gated) — or on a short *complete* utterance: a speech burst of ≥ ~160 ms that ends in silence counts as an answer, so one-word replies like "Boo" or "yes" land instantly instead of timing out (a clink is a single-frame blip and still can't start a turn). The onset is buffered and flushed so your first word survives whole, and the proven RMS machinery still endpoints the finish. Until real speech shows up, the node sends the server *nothing* — a silent window expires locally (`followup_timeout` + the end cue) as a session that never happened. Falls back to sustained-energy gating if the VAD model is missing. New live-tunable keys: `dialog_onset_ms`, `dialog_onset_vad_threshold`.

### Added

- **Instant greetings and a conversational bail-out.** "Hello," "good morning," "goodnight," "howdy," "what's up" now get a warm, varied reply the instant you finish — no model round-trip — and, like the rest of the fast path, they work with no internet at all. "Never mind" / "forget it" gets a quick "Okay, no problem" and cleanly ends a held dialog. (Gratitude — "thanks" — is deliberately *not* fast-pathed: speech-to-text hallucinates those words from noise, so a canned "you're welcome" would fire on phantoms; the LLM handles thanks harmlessly.)
- **More everyday commands skip the model.** Coin flips, dice rolls, and number picks ("flip a coin", "roll a d20", "roll 3d6", "pick a number between 1 and 10") now answer instantly with no LLM call — the logic was always deterministic, so there was no reason to consult one. And common weather questions ("what's the weather", "temperature outside", "what's the forecast for the week") route straight to the weather service, skipping the model's tool-selection step (a noticeable speed-up, though it still fetches). All of these match the bare phrasing only — "flip a coin to decide whether I should repaint" or "weather in Paris" keep going to the LLM, which can actually reason about them.
- **`params:` in the LLM config — the latency knobs.** Extra parameters merged into every model call: anything LiteLLM accepts (`reasoning_effort`, `service_tier`, `temperature`, `max_tokens`, …), with unsupported ones dropped per-provider so the block is safe with local and non-reasoning backends. `reasoning_effort` and `service_tier` get dashboard dropdowns; both ship as `""` = **don't send the parameter** (models with adaptive defaults, like gpt-5.1, gain nothing from an explicit reasoning value — set `none`…`high` only to force a level on models whose default reasoning is heavier). Credential and routing keys are ignored in this block by design.

### Fixed

- **Back-to-back TTS sessions can no longer bleed into each other.** TTS audio frames now travel through the node's command queue alongside `tts_start`/`tts_end`, so wire order is preserved end-to-end — previously, frames of a next stream (e.g. a timer firing right behind an announcement) could race ahead of the previous stream's end and be appended to the wrong session's audio. One queue, one order; stale frames outside a live session are dropped. (Intercom audio keeps its direct low-latency path.)
- **The dashboard no longer re-parses per-node YAML on every broadcast.** Effective node config reads are mtime-cached (invalidated on writes), so metrics ticks and state changes stop costing filesystem work per node per snapshot.

### Development

- `_index_from` (the static-format device-map builder) moved out of the Home Assistant skill into the resolver test-suite — production builds device indexes only from the live HA topology; the static-format files remain purely as the tests' fixture corpus.

## [3.5.1]

### Added

- **`hardware_aec` — honest half-duplex support for rooms without echo-cancelling speakers.** Kenzy has always assumed AEC speakerphones (they're what let her hear you over her own voice); several features silently depended on it. That assumption is now declared per node (`hardware_aec`, default `true` — nothing changes for existing setups) and honored everywhere: with `false`, wake words are ignored while the node is emitting audio (no more self-interruption on echo-y hardware), **intercom is politely refused** (a two-way call without AEC is a feedback loop), and **alarms are refused at set time** with a spoken offer of a timer or reminder instead — an alarm's ring loop can only be silenced by voice, which can't be heard over the ringing. An already-set alarm on such a room still fires once, timer-style, rather than not waking you at all. Refusals happen *in the reply itself* (the skills know each room's capability), not as an after-the-fact correction. Timers, reminders, announcements, commands, and dialogs work normally everywhere; the dashboard marks half-duplex rooms with a "no AEC" badge. (This is stage 0 of `design/conversational-flow.md` — the groundwork for natural dialog turn-taking.)
- **Skills can be disabled by module — and the Skills tab groups them that way.** There was no toggle that actually meant "Home Assistant": the registry only knew function names (`handle_home_control`, `add_to_list`, …), so nothing mapped to the feature as a whole — and disabling a skill didn't even silence its fast intent. `skills.disabled` now also accepts **module names** (`home_assistant`, `lists`, …), which disable every skill *and* fast intent the file defines; a module also counts as disabled when every one of its skills has been individually switched off. The Skills tab is redesigned around this: modules are **collapsible accordion groups** (distinct headers with chevron, skill/fast-intent counts, and the group toggle; members expand on click), and feature gates — the lists skills' HA requirement, the HA screen's banner — now key off real module state. A `skills.disabled` entry that matches no skill, fast intent, or module now logs a loud warning naming the known modules, instead of silently disabling nothing (and a bare string value is treated as one name rather than iterating into characters). The group toggle operates on the module's members in both directions — "Enable all" works even when the module reads as disabled only because every member was switched off one-by-one. The `lists` group shows its dependency explicitly — "Requires: home assistant" — and is badged **inactive** whenever the `home_assistant` module is off, even though its own toggle is untouched (mirrors the runtime hard gate).
- **The Home Assistant screen is honest about skill state.** With the `home_assistant` skill disabled, the dashboard's HA tab now shows a clear banner — voice control and lists are off, nothing here takes effect until it's re-enabled — while staying fully editable, so curation can be staged before flipping the skill on. With no `HA_API_KEY` configured at all, the tab shows step-by-step onboarding guidance instead of a raw error. And the Skills tab now warns about blast radius *before* you toggle: the `home_assistant` row notes that it also powers the HA screen and shopping/to-do lists.
- **Node system metrics on the fleet view.** Each room's card now shows live CPU, RAM, and disk usage plus SoC temperature (highlighted when it reaches Pi-throttling territory, ≥80°C). Zero new dependencies — nodes read Linux procfs/sysfs directly and report every ~30 seconds over the existing WebSocket; metrics a platform doesn't expose are simply omitted. Metrics updates deliberately don't wake the MQTT/Home Assistant bridge.

### Removed

- **Retired the legacy static device-map fallback.** The pre-live-topology `device_ids.yaml`/`.json` path is gone: the code that read the hand-built files, the `device_ids_yaml`/`device_ids_json`/`device_overlay` config keys, and their fields in the dashboard's llm service editor. The fallback only ever fired when HA was unreachable with nothing cached — a scenario where a stale map could *resolve* a command but never *actuate* it (HA is down), so it converted an honest "couldn't reach Home Assistant" into a slower failure. Live topology + curation.yaml is the one path now; a cached topology still carries the skill through brief HA outages.

### Fixed

- **Kenzy waits after asking you a question.** When her reply *was* a genuine question she's waiting on — you asked her to quiz you, ask you something, or play a back-and-forth — she sometimes ended the exchange anyway, treating "ask a question" as complete once asked. The floor-hold guidance now covers a reply that is itself a question you're waiting to answer, while still not holding for reflexive offers of more help or sign-offs.
- **The default system prompt no longer references a nonexistent tool.** It instructed the model to "only call `get_device_states`" for status questions — a tool that doesn't exist (status checks are `handle_home_control`'s job, and its description says so). Reworded so the model isn't reserving a phantom. If you've customized `system_prompt` in your llm override, check it for the same inherited line.
- **"Tell the house it's time for dinner" no longer answers the clock.** The time/date fast intent gated on a bag of words ("tell"/"what" + "time" anywhere in the utterance), so announce phrasings that merely *mention* time were hijacked into a current-time answer. The classifier now matches anchored whole-utterance patterns — "time" must *be* the question, not the object of a larger command — and qualified questions ("what time is it in London", "what time does the game start") correctly fall through to the LLM, which can actually answer them.

- **Announcements no longer clip the first word.** A race in the node's receive path: binary TTS frames are queued the instant they arrive off the socket, while `tts_start` waits for the command loop — so an announcement's own leading frames could land in the queue first and be eaten by `_begin_tts`'s stale-frame drain (intermittent, worst on busy Pis and on `announce()`'s tight multi-node burst). Queue cleanup now happens only where genuinely stale data can exist — session abort and connection teardown — so the head frames always survive to playback. Regression-tested (`tests/test_tts_head_clipping.py`).

## [3.5.0]

### Changed

- **`OPENAI_API_KEY` is no longer sent to a custom `base_url`.** With `base_url` set in the LLM config (or a skill's `base_url` override), requests to that endpoint now carry `CUSTOM_LLM_API_KEY` (new, optional) or a harmless placeholder — never your OpenAI key. `base_url` is dashboard-editable by design, and previously a repointed URL received the OpenAI key in the Authorization header (see F-14 in the security design doc); now a changed endpoint can't leak it. Local providers (Ollama, LM Studio) need no key and are unaffected, as are all default-endpoint setups. **Migration:** if you point `base_url` at a hosted proxy (LiteLLM proxy, OpenRouter) with its key stored in `OPENAI_API_KEY`, move that value to `CUSTOM_LLM_API_KEY` in `.env`.
- **`dashboard.controls` and `dashboard.logs` now default to on.** Previously the shipped `server.yaml` enabled both, but a config that *omitted* the keys (a partial or hand-written one) silently fell back to a read-only dashboard with no log viewer. The in-code fallback now matches the shipped config, so enabling the dashboard gets you the full experience; set either to `false` to opt out (read-only dashboard / no in-memory logs and transcripts). `dashboard.enabled` itself is unchanged — a config with no dashboard block still wires up nothing.

### Added

- **A complete example skill.** `examples/skills/example_skill.py` is one runnable, heavily commented file demonstrating the whole authoring surface — an LLM tool, an instant fast intent, per-skill config, server-injected context, and a server action — with try-it steps in its header. It's loaded by the test suite through the real overlay loader, so it stays correct as the API evolves. Linked from the [Writing Skills](https://docs.kenzy.ai/skills/writing-skills/) guide.
- **Silent local fallback for the cloud stages.** Each cloud-backed stage can now retry locally when the cloud call fails — silently, with no double error handling: if the fallback fails too, the user just gets the spoken error cue. **LLM:** an optional `fallback.model`/`fallback.base_url` in the LLM config (e.g. a local Ollama model) — the whole tool-calling request pins to the fallback after the first primary failure, so a multi-step request doesn't re-pay the cloud timeout per step, and skill sub-calls (news summaries, the HA resolver) ride the same path. **TTS:** with `openai.fallback: true` (the default), a cloud synthesis failure retries with local Kokoro when the `kokoro` extra is installed (lazy first load). **STT:** with `openai.fallback: true` (the default), a cloud transcription failure retries with local faster-whisper, loaded lazily on first need. Fallback calls never carry cloud credentials (same rule as custom endpoints).
- **Kenzy says so when a request fails.** A mid-pipeline failure (STT, LLM, or TTS down/erroring) used to be a log line — from the couch, indistinguishable from being ignored. The node now plays a **pre-recorded** spoken cue ("I'm sorry, but I'm having trouble processing your request at the moment"), pre-recorded in Kenzy's own voice precisely because it must work when TTS is the broken part. Configurable per node like the other sounds (`sound_error`; a path is read on the server host; empty = stay silent), applied live with no restart. A failed reply also releases any held multi-turn conversation.
- **"Running Fully Local" guide.** The docs now articulate what the architecture always supported: every voice-pipeline stage — wake word, STT, reasoning, TTS, speaker ID — running on your own hardware with nothing spoken at home leaving your network. Stage-by-stage recipe (Ollama for the LLM, Kokoro for the voice, whisper already the STT default), the honest hardware note (the LLM is the stage that wants a GPU), which skills still make outbound requests and how to disable them, and a pull-the-plug verification checklist.
- **Shopping & to-do lists by voice.** "Add milk to the shopping list", "what's on the list?", "check off eggs", "take bread off the list" — instant (deterministic fast path) for the everyday phrasings, with LLM tools for compound requests ("add everything I need for pancakes"). Lists live in **Home Assistant** (`todo` entities) rather than in Kenzy, so they're on everyone's phone via the HA companion app, and local lists and synced backends (Google Tasks, Todoist, CalDAV) all work identically — with **no list yet**, an add command makes Kenzy offer to create one ("Should I create one called Shopping list?") — created in HA on your spoken **yes**, never implicitly, with a fallback to the manual two-click **Local to-do** instruction if HA declines; "create a camping list" works directly too. Everything list-related is hard-gated on the Home Assistant connection (no `HA_API_KEY`, or the `home_assistant` skill disabled ⇒ list phrases are untouched). A new **Lists** section in the dashboard's Home Assistant tab picks which list a bare "the list" means and adds spoken aliases ("the groceries") — stored in `curation.yaml`'s new `lists:` block. With one list it's the default automatically; with several and no default, Kenzy asks which — and holds the mic open for the answer.
- **Backup & restore.** The dashboard's Settings page gains a **Download backup** button — a single `.tar.gz` of the deployment's state: per-node and per-service settings, `server.local.yaml`, Home Assistant curation, **enrolled voice profiles** (the un-regenerable part), custom skills, `constraints.txt`, and scheduled timers/alarms/reminders. The archive is **complete even on a multi-host deployment**: the stateful services (speaker, LLM) expose token-gated `GET /backup` slices that the server fetches and merges — so remote voice profiles and skills are captured too (an unreachable service degrades to a partial archive, recorded in the manifest). Restore onto a fresh install with **`kenzy-init --restore <file>`** — it refuses to overwrite existing files (listing the collisions, writing nothing) unless `--force`, and rejects unsafe archives (path traversal, symlinks, unexpected paths; a service slice can never inject configs or a `.env`). By default `.env`/API keys and `models/` stay out; two opt-in checkboxes widen the scope — **include secrets** (a true one-file recovery; the archive then carries live credentials, treat it like a password) and **include everything** (adds `models/`, capturing hand-placed custom models). "My SD card died" is now a five-minute recovery instead of a re-enroll-everyone event.
- **Write-only API-key entry in the dashboard.** Settings gains an **API keys** section: choose `OPENAI_API_KEY` / `HA_API_KEY` / `HF_TOKEN` (or a custom name), paste a value, and it's upserted into the server host's `.env` — mirroring the change-password form's trust model: **values are never displayed, served, or logged**; the page shows only which names are set. Restart the affected services to apply (co-located services share the server's `.env`; remote hosts keep their own — `kenzy-deploy` syncs it). Gated by `dashboard.controls`. This removes the last mandatory terminal step (`nano .env`) from a default single-box setup.
- **Timers, alarms, and reminders.** "Set a timer for 10 minutes", "wake me at 7 every weekday", "remind me in 20 minutes to flip the bread" — set, check, and cancel by voice. Entries live on the **server** (persisted to `data/schedules.json`, so they survive a restart) and fire in the room that set them, or in another room you name ("wake me at 7 **in the bedroom**"). A timer plays a **tone** then its announcement; an alarm **rings — tone + announcement — until acknowledged** with the wake word (capped ~4 minutes); a reminder speaks its text ("You asked me to remind you to take the dog out."). The tones are per-room settings (`sound_timer`/`sound_alarm`, bundled defaults, empty = voice-only) editable from each node's dashboard config like the other sounds — but mixed in **server-side**, so changes apply live with no restart, and the alarm tone still sounds when TTS synthesis is down. Alarms and clock reminders can recur (daily / weekdays / weekends / specific days). **Any command can be deferred** the same way — "turn on the porch light in 30 seconds", "lock the front door at 10:30 pm" — stored as a scheduled command and **replayed through the normal intent pipeline at fire time**, as if spoken then in that room (same fast path, same skills, same spoken confirmation), carrying the speaker identity from when it was set so voice-gated actions stay authorized. Deferred commands are deliberately one-shot: a recurring one is a standing automation, which is Home Assistant's job. The common phrasings are handled by the deterministic fast path — instant, no model call — including status ("how much time is left?") and cancellation, with a clarifying question when "cancel the timer" is ambiguous; anything fuzzier falls through to new LLM tools. The dashboard gains a **Scheduled** tab (live countdowns, per-entry Cancel) that **updates in real time** — entries set by voice appear, and fired/cancelled ones disappear, without a refresh — and the server injects each room's active entries into the language-model context so it can answer schedule questions naturally. A missed one-shot entry (server down at fire time) is spoken up to 5 minutes late, otherwise dropped; recurring alarms simply advance.
- **Cloud speech-to-text option.** The STT service now supports two providers, selected by `provider` in its config (editable from the dashboard's Services → stt tab, shown as a dropdown): **`whisper`** (the default — local faster-whisper, unchanged, nothing leaves your network) or **`openai`** (OpenAI's transcription API: `gpt-4o-mini-transcribe` by default, `gpt-4o-transcribe`/`whisper-1` selectable). The cloud provider loads no local model at all, so `kenzy-stt` runs comfortably on underpowered hardware; it reuses the `OPENAI_API_KEY` the default TTS/LLM setup already needs. Trade-off, stated plainly: with `provider: openai`, everything captured after the wake word is sent to OpenAI for transcription — stay on `whisper` if spoken audio must remain on your own hardware. `/health` now reports the active provider and model on the dashboard's fleet view.

- **Multi-turn conversation (assistant holds the floor).** When a reply is deliberately incomplete and needs the user's answer to finish the task — the classic being a knock-knock joke ("Knock knock." → "Who's there?") — Kenzy now re-opens the mic for the follow-up **without requiring the wake word again**, reusing the prompt-then-capture primitive from voice consent/enrollment. Scoped conservatively: chat models reflexively try to keep every conversation going, so the floor is only held when the model explicitly sets `expect_response`, and reflexive closers/clarifiers ("is there anything else?", "sorry, can you clarify?") are hard-suppressed so a normal reply never leaves the mic open. The dialog ends on a completed reply, silence, a stop phrase ("never mind"), or a safety cap on consecutive held turns. When a held dialog concludes with its final spoken reply, the node can play an end-of-dialog cue (new `sound_dialog_end`, **off by default**; set it to a sound such as `disconnect.wav` to enable, configurable like the other sounds) — and it never sounds after a plain single-turn reply.
- **Web search skill.** The LLM can now search the web for current, live, or niche information it can't answer from its own knowledge (recent events, prices, "look it up…"). Two backends, selected by `skills.web_search.provider`: **DuckDuckGo** (default — keyless, zero setup, via the new `ddgs` dependency) or a self-hosted **SearXNG** instance (nothing leaves your network; point `skills.web_search.searxng_url` at its `/search` endpoint with the JSON format enabled). All options (provider, max results, timeout, region, SearXNG URL) are editable from the dashboard's Services → llm tab, with the provider shown as a dropdown.

## [3.4.2]

### Fixed

- **"Turn off the lights downstairs" only affected the current room** (or missed entirely). The Home Assistant resolver modeled areas (rooms) but had no concept of **floors**, so a floor name like "downstairs" wasn't recognized as a scope — the request fell through the fast path to the LLM, which was biased back toward the node's own room. Floors are now first-class in the fast path: a floor name expands a bare-group command ("the lights") to **every area on that floor**, independent of which room the node is in. Area-level ("in the office") and implicit-room ("the lights") commands are unchanged. The LLM fallback's prompt was also corrected — it described the topology as `area > room` when it's actually `floor > area`, which hurt floor-level status queries.
- **Fast replies clipped the start of the spoken confirmation** (e.g. only "…office lights" instead of "I turned off the office lights"). On the fast path the server's reply could arrive while the node was still starting its "processing" sound, and the two ran on different async tasks; the chime→TTS handoff was a non-atomic `abort()`+`play()` pair whose flag writes the audio callback could interleave with, dropping the reply's opening words. TTS playback now swaps in **atomically from the first sample** (a single interrupt flag, regardless of what's currently playing), and the "processing" sound is suppressed once a reply has already begun — so a fast confirmation always plays in full.

## [3.4.1]

### Fixed

- **The bundled wake-word model (`hey_ken_zee.tflite`) was missing from the published package**, so a fresh node that relies on the bundled default (rather than a separately-synced model) failed to start with "could not find pretrained model …". The model file had never been committed to git, so package builds from a clean checkout omitted it; it's now tracked and ships in the wheel/sdist.

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