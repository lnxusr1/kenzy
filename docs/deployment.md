# Deployment

`kenzy-deploy` manages Kenzy installations across a fleet of remote hosts over SSH. It handles OS setup, Python virtualenv creation, package installation, systemd unit installation, and service management.

## Install modes

`kenzy-deploy` supports two install modes, set by `install_mode` in `deploy.yaml` (top-level default, overridable per host, or with CLI flags):

- **`source`** (default) — *source-push*. Rsyncs your local tree (`src/`, `configs/`, `pyproject.toml`, plus the `skills/`/`data/`/`models/` paths) to each host and runs an editable install (`pip install -e '{install_path}[extras]'`). Edit a skill or config locally, run `kenzy-deploy upgrade`, and it's live everywhere — no package publish in the loop.
- **`pypi`** — installs `kenzy[extras]` from PyPI (pinned to `version:`/`--version`, else the latest `>=3`). Only `configs/` and the per-host `sync` paths are pushed; the code comes from PyPI. `upgrade` adds `-U`.

CLI overrides: `--local` forces `source` for every host; `--version X` overrides the pinned PyPI version.

Run `kenzy-deploy` from a directory whose `configs/deploy.yaml` it can find: the rsync base ("config-root") is derived from the `deploy.yaml` location (`<root>/configs/deploy.yaml` → `<root>`), **not** `pyproject.toml` — so `pypi` mode works from an operational tree (config + skills + data) with no source checkout, while `source` mode still expects a repo checkout there. The [`install.sh`](https://kenzy.dev/install.sh) one-liner sets up a control machine, or use a git checkout.

## Prerequisites

On each remote host:

- **SSH key authentication** — passwordless login from your dev machine
- **Passwordless sudo** — add to `/etc/sudoers`:
  ```
  pi ALL=(ALL) NOPASSWD: ALL
  ```
- Python 3.11+ installed (or use `python_bin: python3.11` in `deploy.yaml`)

## Configuration

Edit `configs/deploy.yaml`:

```yaml
install_mode: source      # source (rsync + pip -e) or pypi (pip install kenzy)
# version: 3.1.0          # pypi mode only; omit for the latest 3.x

defaults:
  ssh_user:     pi
  install_path: /opt/kenzy
  python_bin:   python3

  # Files and directories synced to every host on install/upgrade.
  sync:
    - .env

# Extra paths synced only to hosts running specific services.
service_sync:
  llm:     [skills, data/home_assistant]
  speaker: [data/speakers]

hosts:
  main-server:
    address:  192.168.1.100
    services: [server, stt, tts, llm]

  living-room:
    address:  192.168.1.10
    services: [node]

  bedroom:
    address:  192.168.1.11
    services: [node]

  speaker-box:
    address:  192.168.1.30
    services: [speaker]
```

### Host options

| Key | Default | Description |
|---|---|---|
| `address` | — | IP address or hostname |
| `services` | — | List of services to install on this host |
| `ssh_user` | *(from defaults)* | SSH username |
| `install_path` | *(from defaults)* | Remote installation directory |
| `python_bin` | *(from defaults)* | Python executable name |
| `local` | `false` | Set `true` for the local machine (no SSH used) |
| `sync` | `[]` | Additional paths synced to this host specifically |
| `install_mode` | *(from top-level, `source`)* | `source` or `pypi` for this host |
| `version` | *(from top-level)* | PyPI version to install in `pypi` mode |
| `constraints` | *(auto: `constraints.txt` at the config-root)* | pip constraints file (relative to the config-root or absolute) of dependency pins to honor on install **and upgrade** for this host |
| `pip_packages` | `[]` | Extra packages to install after the main install (e.g. host-specific add-ons) |

!!! tip "Pinning a dependency on a specific host"
    Put version pins in a constraints file and point `constraints:` at it (or just drop a `constraints.txt` at the config-root for fleet-wide pins). `kenzy-deploy` pushes it to the host and passes it with `-c` on every install/upgrade — so a host that needs, say, a specific `transformers` keeps it across `kenzy-deploy upgrade` instead of having it moved. This mirrors the per-user install's `constraints.txt`.

### Path syncing

The `sync` key (in `defaults` and per-host) and `service_sync` (per-service) accept paths relative to the project root. Both files and directories work:

- **File** (`configs/server.yaml`): synced to the same relative path on the remote host
- **Directory** (`skills/`): synced recursively with `--delete`

Paths in `service_sync` are merged with any host-specific `sync` entries. The `.env` file in `defaults.sync` ensures every host gets the latest secrets on each upgrade.

## Commands

### `kenzy-deploy init`

One-time OS setup on all hosts. Installs system packages (`libportaudio2`, etc.) and creates the install directory.

```bash
kenzy-deploy init
kenzy-deploy init --host living-room   # single host
```

### `kenzy-deploy install`

First full deployment:

1. Syncs the tree to `install_path` — full source in `source` mode, `configs/` only in `pypi` mode
2. Creates a Python virtualenv at `install_path/.venv`
3. Installs the package with the appropriate service extras (editable from source, or from PyPI)
4. Syncs skill/data directories per `service_sync`
5. Syncs `.env` and any other `sync` paths
6. Generates and installs systemd unit files
7. Enables and starts all services
8. Downloads inference models (`kenzy-setup`)

```bash
kenzy-deploy install
kenzy-deploy install --host main-server
```

### `kenzy-deploy upgrade`

Push an update to running hosts:

1. Syncs the updated tree (source, or `configs/` in `pypi` mode)
2. Reinstalls/updates the package (editable from source, or `pip install -U` from PyPI)
3. Re-syncs skills, data, `.env`, and other configured paths
4. Restarts all services

```bash
kenzy-deploy upgrade
kenzy-deploy upgrade --host living-room
```

### `kenzy-deploy status`

Check whether each service is running on each host.

```bash
kenzy-deploy status
```

### `kenzy-deploy logs`

Tail the systemd journal for a service on a specific host.

```bash
kenzy-deploy logs llm --host main-server
kenzy-deploy logs node --host living-room
```

## Per-host configuration

All hosts receive the same base `configs/` directory on every install and upgrade. To give a specific host different settings — a host-local `audio_device`, a different LLM model, etc. — create a per-host overlay directory:

```
configs/
  node.yaml                      ← base defaults, sent to all hosts
  hosts/
    living-room/
      node.yaml                  ← overrides for living-room only
    bedroom/
      node.yaml                  ← overrides for bedroom only
    main-server/
      llm.yaml                   ← different model or system prompt
```

On each `install` or `upgrade`, after syncing the base `configs/`, the deploy script checks for `configs/hosts/<host-name>/` and copies any files it finds into `{install_path}/configs/` on that host, replacing the base version of each file completely. Config files not present in the overlay are left as the base version.

!!! note "Complete files required"
    The overlay is a file-level replacement, not a key-level merge. A host-specific config file must be complete and valid on its own — the service will not fall back to base `configs/` values for keys that are missing. The recommended approach is to copy the full base config file into the overlay directory and change only the lines that differ.

!!! tip "Nodes: prefer config-pull"
    A node's operational config (audio device, wake-word thresholds, VAD timing, sounds, **and its room name**) is server-owned and pulled on connect — you usually don't need a per-host node overlay at all. Set `node_defaults` in the server's `server.yaml` and per-node values (including `room_id`) in `configs/nodes/<node_id>.yaml`; the server pushes them to each node when it connects. Pre-seed a not-yet-deployed device by assigning its `node_id` at install (`kenzy-init --node-id`) and creating that override file ahead of time. Reserve the deploy overlay for cases where you must pin a value *before* the node can reach the server. See [Discovery & config-pull](configuration/server.md#discovery-and-config-pull).

```yaml
# configs/hosts/living-room/node.yaml  (full copy of configs/node.yaml, with these lines changed)
audio_device: "plughw:CARD=speakerphone,DEV=0"
# ... all other keys from the base node.yaml must also be present
```

## Workflow

```
Initial setup
─────────────
1. Edit configs/deploy.yaml with your hosts
2. Set up SSH keys and passwordless sudo on each host
3. kenzy-deploy init
4. kenzy-deploy install

Ongoing updates
───────────────
1. Make changes locally (code, skills, configs, .env)
2. kenzy-deploy upgrade
```

## Systemd integration

Each service runs as a systemd unit named `kenzy-<service>`. Unit files are generated from templates and written to `/etc/systemd/system/`. Services are configured to restart automatically on failure.

To manage a service manually on a remote host:

```bash
ssh pi@192.168.1.100 "sudo systemctl status kenzy-node"
ssh pi@192.168.1.100 "sudo systemctl restart kenzy-llm"
ssh pi@192.168.1.100 "sudo journalctl -u kenzy-stt -f"
```
