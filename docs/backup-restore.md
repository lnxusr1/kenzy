# Backup & Restore

Your Kenzy server accumulates state you'd hate to lose — most of it recreatable
with some effort, one part not at all:

- **Enrolled voice profiles** — the only way to "restore" these without a backup
  is re-enrolling every person in the house.
- Per-room node settings (rooms, calibrated thresholds, sounds, volume)
- Backend service settings (models, voices, skills configuration)
- Your Home Assistant curation (aliases, room defaults, exclusions)
- Custom skills and dependency pins (`constraints.txt`)
- Active timers, alarms, and reminders

A backup is a single small `.tar.gz` of all of it — and it's **complete even when
your services run on different machines**: the server fetches the voice profiles
from the speaker host and the skills/curation from the LLM host and merges them
into the one archive. (If a service is unreachable at backup time, the archive is
still produced and the gap is recorded in its manifest.)

## Downloading a backup

Open the dashboard → **Settings → Backup → Download backup**. That's it — the
file (`kenzy-backup-<date>.tar.gz`) downloads to your browser. Keep a copy
somewhere that isn't the server.

Two things stay out **by default**, each with an opt-in checkbox:

- **Include secrets** adds `.env` (your API keys). Off by default because the
  archive then carries **live credentials** — whatever your setup keeps there,
  which for most installs means keys that can spend money with a cloud provider
  or control your smart home. Turn it on for a true one-file recovery, and then
  treat the file like a password: keep it somewhere encrypted, not in a synced
  Downloads folder.
- **Include everything** adds `models/`. Off by default because models are bulky
  and `kenzy-setup` re-downloads them — but turn it on if you've placed a custom
  model on the server that exists nowhere else.

## Restoring

On a fresh install (after running the [installer](getting-started.md) or
`kenzy-init`):

```bash
kenzy-init --restore kenzy-backup-20260703-120000.tar.gz
```

Then finish up:

1. Add your API keys to `~/.config/kenzy/.env` (skip if the backup was made with
   **Include secrets** — they're already restored)
2. `kenzy-setup` (re-download models; skip for an **Include everything** backup)
3. Restart the services: `systemctl --user restart 'kenzy-*'`

!!! note "TLS survives a restore"
    A backup never contains your TLS certificate or private key (private-key
    material stays on its host, like `.env`). If the restored config had TLS
    enabled, `kenzy-init --restore` mints a fresh self-signed certificate in
    its place, so the restored server keeps speaking `https`/`wss` — Kenzy's
    clients don't pin certificates, so a new one is seamless.

On a **multi-host** deployment, restore the same archive on each host — every
service reads only its own part of the config home, so one archive serves them
all. (Custom wake-word model files living on room nodes are the one thing outside
the archive — keep your own copy of those.)

Your rooms, tuning, voice profiles, curation, and skills are back; room nodes
reconnect and pull their restored settings automatically.

!!! note "Restore won't overwrite without permission"
    If any file in the archive already exists in the config home, the restore
    refuses and lists the collisions — nothing is written at all. Re-run with
    `--force` to overwrite (e.g. restoring over a freshly scaffolded config
    home, which is the common case).

## Good habits

- Download a fresh backup after enrolling voices, curating Home Assistant
  devices, or any calibration session you'd rather not redo.
- Before a big upgrade or hardware change, grab one — it's one click.
- The archive contains your configuration (including the node join token), so
  treat it like the config itself: private, not something to post publicly.
