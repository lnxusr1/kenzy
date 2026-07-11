# Troubleshooting

Find your symptom below. Most problems show up in one of three places, so when in
doubt, look there first:

- **The dashboard's fleet view** (`http://<server>:8770`) — is every service green?
  Does the node's card show any warning badge?
- **The dashboard's Logs tab** — pick the server, a service, or a node as the
  source and read the most recent lines.
- **The service journals** on the machine itself:

    ```bash
    systemctl --user status 'kenzy-*'          # what's running / what crashed
    journalctl --user -u kenzy-server -f       # follow a service's log live
    ```

    (Unit names are `kenzy-server`, `kenzy-node`, `kenzy-stt`, `kenzy-tts`,
    `kenzy-llm`, `kenzy-speaker`.)

---

## The dashboard won't load

- **Is the server running?** On the server machine: `systemctl --user status
  kenzy-server`. If it's stopped or crash-looping, `journalctl --user -u
  kenzy-server -n 50` shows why.
- **Right address?** The dashboard is on port **8770** — `http://localhost:8770` on
  the server itself, or `http://<server-ip>:8770` from another machine.
- **Reachable from another machine?** The default bind (`0.0.0.0`) allows LAN
  access, but if `dashboard.bind` in `server.yaml` was set to `127.0.0.1`, the
  dashboard is only visible on the server itself. A host firewall (e.g. `ufw`) can
  also block the port.

## I can't log in to the dashboard

The default login is **`admin` / `password`**. If it was changed and forgotten,
reset it on the server machine:

```bash
kenzy-passwd     # prompts for a username and new password
```

Then restart the server (`systemctl --user restart kenzy-server`).

## A service shows unhealthy in the dashboard

Click into **Services**, select the unhealthy one, and check its log (Logs tab →
that service). The usual causes:

- **Missing API key** — the TTS and LLM services need `OPENAI_API_KEY` (or your
  chosen provider's key) in `~/.config/kenzy/.env` **on the machine where that
  service runs**. After editing `.env`, restart the service:
  `systemctl --user restart kenzy-tts kenzy-llm`.
- **First start is slow** — STT and speaker-ID download their models on first
  run; give a fresh install a few minutes before judging it.
- **It's waiting for the server** — backend services fetch their settings from
  `kenzy-server` at startup and wait until it answers. If the server is down or
  unreachable, they sit in a retry loop. Start/fix the server first.
- **Restart it** — the Services tab has a per-service **Restart** button; from a
  terminal, `systemctl --user restart kenzy-<name>`.

## Kenzy doesn't hear the wake word

No chime when you say "hey Kenzie":

- **Check the node's card** in the fleet view. An **"audio failed"** badge means the
  node couldn't open its microphone — usually a wrong or missing `audio_device`.
  Fix it from the browser: Configure → **Set up / calibrate audio…** → pick the
  right device (the node restarts itself and comes back on the new device).
- **Wrong microphone selected.** If there's no badge but it still can't hear you,
  the node may be listening on the wrong device (e.g. a webcam mic instead of your
  speakerphone). Same fix: the audio wizard's **Device** step.
- **Threshold too strict.** Run the wizard's **Wake word** step — say "hey Kenzie" a
  few times and it measures your actual scores and applies a threshold just below
  them. This beats hand-editing numbers.
- **Say it naturally.** "Hey Kenzie" as one relaxed phrase works better than
  exaggerated robot-speak.
- **Is the node even connected?** If the card is missing or grayed out, see
  [A node never appears](#a-node-never-appears-in-the-dashboard).

## It cuts me off mid-sentence (or keeps listening forever)

The silence detection is tuned wrong for your room. Recalibrate — either say
**"Hey Kenzy, calibrate"** and follow the spoken instructions, or run
**Set up / calibrate audio…** from the dashboard. Calibration measures both the
room's quiet floor *and* your voice level (the wake-word repetitions double as
the voice sample) and anchors `silence_rms_threshold` to your voice, so a noisy
appliance starting up later stays below it.

- Set **too high**, quiet speech counts as silence — you get cut off.
- Set **too low**, background noise counts as speech — it never detects that
  you stopped talking.

If calibration reports **poor separation**, the mic can't reliably tell your
voice from the room's noise — move it closer to where people talk.

## It hears me but never replies

First, listen for the apology: when a request fails mid-pipeline, Kenzy plays a
pre-recorded *"I'm sorry, but I'm having trouble processing your request"* cue —
so hearing **that** means the node and server are fine and one of the backend
stages failed (keep reading). Hearing **nothing at all** points earlier in the
chain: the node↔server link, or the cue was disabled (`sound_error` in the
node's settings). Work down the chain:

1. **Open the dashboard's Activity tab.** If your words appear as a transcript,
   speech-to-text worked and the problem is further along (LLM or TTS). If nothing
   appears at all, check the STT service.
2. **Check the services' health** on the fleet view — an unreplying pipeline is
   very often one unhealthy service. See
   [A service shows unhealthy](#a-service-shows-unhealthy-in-the-dashboard).
3. **API key and internet.** With the default OpenAI setup, the LLM and TTS calls
   need a valid `OPENAI_API_KEY` and a working internet connection. The LLM/TTS
   service logs will show authentication or connection errors plainly.
4. **Read the server log** (Logs tab → server): it reports each stage (STT →
   LLM → TTS) and which one failed or timed out.

## It wakes up randomly / interrupts itself / hears its own voice

- **Echo is the classic cause**: the microphone picks up Kenzy's own speech from
  the speaker. The real fix is hardware — a **USB speakerphone with built-in echo
  cancellation**. Separate mic + speakers will misbehave, and Kenzy does not do
  echo cancellation in software.
- **Random false wakes** (TV, music, silence): run the audio wizard's **Wake word**
  step and apply the suggested **VAD threshold** — it discards wake-word hits that
  don't coincide with actual voice activity, and lets you keep good sensitivity for
  real speech.

## A node never appears in the dashboard

You installed a room node but its card never shows up:

- **Same network?** The node finds the server via mDNS (multicast), which doesn't
  cross VLANs or some Wi-Fi isolation setups. If your network segments traffic,
  set the server address explicitly in the node's `node.yaml`:

    ```yaml
    server_url: "ws://192.168.1.100:8765"
    ```

- **Wrong or missing join token.** If the server has a `discovery.token` (installer
  default), the node must present the same one. Watch the node's log
  (`journalctl --user -u kenzy-node -f`) — a connection that opens and immediately
  closes right after connecting is the token being rejected. Copy the token from
  the dashboard (**Settings → Node provisioning**) into the node's `node.yaml`
  (`discovery.token`), or reinstall the node with `--token`.
- **Is the node service running?** `systemctl --user status kenzy-node` on the
  device.
- **Firewall on the server** blocking port **8765**.

## The node is connected but shows "audio failed"

The node is reachable but couldn't open its audio device — it stays online exactly
so you can fix it remotely. Open its Configure page → **Set up / calibrate
audio…** → pick a device from the list → the wizard saves and restarts the node.
If no devices are listed, check the USB connection and `journalctl --user -u
kenzy-node` on the device.

## Voices are misidentified (or always "unknown")

- Enroll **in the room, on the mic you actually use** — an enrollment done on a
  different microphone doesn't transfer well. Re-enroll from the dashboard
  (**Speakers → Enroll from a room**).
- Tune `identify_threshold` (Services → speaker): **lower** it if enrolled people
  keep coming back as "unknown"; **raise** it if the wrong names come back. See
  [Speaker Enrollment](speaker-enrollment.md#tuning-the-identification-threshold).

## Smart-home commands don't work

- Check that `HA_API_KEY` is set in `.env` on the LLM service's machine and that
  the `url` under `skills.home_assistant` (Services → llm) points at your Home
  Assistant.
- If Kenzy answers but picks the wrong device (or says it can't find one), the
  fix is usually **naming**: give the device a spoken alias in the dashboard's
  **Home Assistant** tab. Full guide: [Home Assistant](skills/home-assistant.md).

## Starting over

**First: grab a backup** if there's anything worth keeping (enrolled voices,
room tuning, curation) — dashboard → Settings → **Download backup**, restorable
later with `kenzy-init --restore` ([details](backup-restore.md)).

The installer removes everything it created:

```bash
curl -fsSL https://kenzy.ai/install.sh | bash -s -- --uninstall           # keep settings
curl -fsSL https://kenzy.ai/install.sh | bash -s -- --uninstall --purge   # remove settings too
```

Then run the install again. Your enrolled voices and settings live in
`~/.config/kenzy` — without `--purge` they survive a reinstall.

## Still stuck?

Open an issue at [github.com/lnxusr1/kenzy](https://github.com/lnxusr1/kenzy/issues)
with what you tried and the relevant log lines (dashboard → Logs, or
`journalctl --user -u kenzy-<service> -n 100`). Transcript-bearing logs are only
kept in memory on your own machines — copy out what you need before restarting.
