# Upgrading

Most upgrades need nothing beyond the upgrade itself. This page covers the ones
that do — the releases where a new feature needs something a package upgrade
can't bring on its own, like a system package or a group membership.

Check the [changelog](https://github.com/lnxusr1/kenzy/blob/main/CHANGELOG.md)
for what's in a release; check here for whether it asks anything of you.

## Before you upgrade

- **Take a backup.** Dashboard → **Settings** → *Download backup*. It carries
  your configs, enrolled voices, memory and skills — see
  [Backup & Restore](backup-restore.md).
- **Version pins are honored automatically.** If you keep a `constraints.txt`
  in the config home, every upgrade path passes it to pip, so an upgrade can't
  silently move a pin you set deliberately.

## How to upgrade

Whichever way you installed:

| Installed with | Upgrade with |
|---|---|
| The one-line installer or `pipx` | The dashboard's **Upgrade** buttons — Fleet → the server, each service chip, and each node — see [Dashboard](dashboard.md) |
| `kenzy-deploy` | [`kenzy-deploy upgrade`](deployment.md#kenzy-deploy-upgrade) (add `--host NAME` for one machine) |
| An editable checkout (`pip install -e`) | `git pull` — source installs don't self-upgrade, and the dashboard's Upgrade buttons are for package installs |

Upgrade the **server first**, then services, then nodes.

## Version-specific steps

Newest first. Anything not listed needs no manual steps.

### 5.1.1

**Nothing is required**, and an upgraded install behaves exactly as before. Two
new settings are worth knowing about, both opt-in:

- **`mic_volume`** is unset by default, so a node's capture gain stays exactly as
  it was until you deliberately set the new key. (Applying it needs `amixer` —
  part of `alsa-utils`, already present on any host with working sound; if it's
  somehow absent, the node's page says so.)
- **`audio_group`** turns on co-audible wake arbitration, and it matters only if
  you have **two or more nodes close enough to hear the same "Hey Kenzy"** — the
  situation where they both wake and answer in unison. Give those nodes the same
  `audio_group` name (any string) in their node settings and the server picks the
  one that heard you best, silencing the rest — one chime, one answer. It's
  live-applied (no restart), off until you set it, and works best when the
  co-audible nodes are the **same model** of speakerphone (different models hear
  the same voice at different levels, which can bias the pick). Nodes that stand
  alone in a room need nothing.

### 5.1.0

Nothing required. Add-ons are new and entirely opt-in: an upgraded install
behaves exactly as before until you deliberately install one (see the
[kenzy-ld2450](https://github.com/lnxusr1/kenzy-ld2450) README for what that
looks like — including the one-time hardware wiring an in-node sensor needs).

### 5.0.8 — an existing install keeps the PyTorch build it already has

New installs on a Linux/x86 machine with no NVIDIA card now get the CPU-only
build of PyTorch, which skips roughly **2.7 GB** of CUDA runtime that machine
could never use. An upgrade will **not** switch you over: pip sees `torch`
already installed and satisfying the requirement, so it leaves it alone. That
is deliberate — an upgrade replacing a working PyTorch is a bigger risk than
the disk it would reclaim.

Nothing is broken if you do nothing. If you want the space back on a machine
with no GPU, remove the virtualenv and re-install:

```bash
rm -rf ~/.local/share/kenzy/venv
curl -fsSL https://kenzy.ai/install.sh | bash -s -- --profile server --yes
```

Your configuration, data and voiceprints live in `~/.config/kenzy` and are
untouched by this. On a machine that **does** have an NVIDIA card, the installer
detects it and keeps the standard CUDA build; `--cpu-torch` and `--cuda-torch`
force either way.

### 5.0.7

No manual steps. Room nodes on Python 3.12 or newer, and on macOS, install the
wake-word engine differently — but the installer handles that for you.

### 5.0.5 and 5.0.6

No manual steps. 5.0.6 adds proactive safety announcements, but they are **off
until you switch them on** — see
[Proactive speech](configuration/server.md#proactive-speech) — so an upgrade
changes nothing about when Kenzy speaks.

### 5.0.4 — volume buttons on existing nodes

New in this release: a USB speakerphone's physical `+`/`−` buttons can move the
room's volume ([how it works](configuration/node.md#speakerphone-volume-buttons)).
New installs get this set up automatically. **Existing nodes don't**, and the
upgrade deliberately won't add it for you: the feature needs `evdev`, which
builds from source, and adding a compile step to an upgrade would break any node
without build tools. So a node that upgrades to 5.0.4 keeps working exactly as
before, and picks the feature up only when you do the following.

Skip this entirely if you don't want volume buttons on that node.

**If the node was installed with the one-line installer or `pipx`**, run these on
it, in order:

```bash
sudo apt-get install -y gcc python3-dev
~/.local/share/kenzy/venv/bin/pip install evdev
sudo usermod -aG input $USER
sudo reboot
```

(Adjust the venv path if you installed with `--venv`.)

**If the node was installed with `kenzy-deploy`**, there's nothing manual —
`mediakeys` is default-on for node hosts as of 5.0.4. From your deploy machine:

```bash
kenzy-deploy init && kenzy-deploy install
```

`init` installs the build packages and adds the SSH user to the `input` group;
`install` adds the extra. **Reboot each node afterwards** (see below). Set
`media_keys: false` on a host, or under `defaults:`, to opt a node out.

!!! warning "The reboot isn't optional"

    Adding yourself to `input` and then restarting the node service does **not**
    work. Supplementary groups are fixed per process when a login session is
    created, so the `systemd --user` manager keeps its old credentials and hands
    them to every service it starts — and lingering (on by default, so nodes
    start at boot) means it survives a logout too. Reboot, or
    `sudo loginctl terminate-user <user>` and log back in.

    Without it the watcher sees *no* input devices at all, and the node page says
    so.

Then enable it per node, from the dashboard:

- **Fleet → the node → the audio setup wizard.** Its device step offers a
  *This device has volume buttons* checkbox whenever the device you picked has
  them, and applies it in the same step. This is the easy path — it fills in the
  device match for you.
- Or set `volume_buttons` in the node's config grid directly, alongside
  `volume_button_device` and `volume_button_step`.

Either way it applies live, with no restart. The node page shows the endpoint's
status — found, not found, or why not.

!!! info "If `evdev` won't build"

    You'll see a compiler error mentioning `Python.h` or `gcc`. It means step 1
    didn't take. Nothing else is affected — the node runs normally and reports
    volume buttons as unavailable.

### 5.0.0 through 5.0.3

No manual steps. If you're coming from 4.x, note that 5.0.0's room presence
needs Home Assistant configured (an HA skill URL **and** `HA_API_KEY`) before it
starts — without both it logs *idle* and stays off. See
[Home Assistant](integrations/home-assistant.md).

## A feature won't turn on after upgrading

Usually a missing dependency rather than a missing setting — the service skips
the feature instead of crashing. The service editor's **feature chips** show
this directly and can install what's missing. Walkthrough:
[Troubleshooting](troubleshooting.md#a-new-feature-wont-turn-on-after-an-upgrade).
