# Deployment

`kenzy-deploy` manages Kenzy installations across a fleet of remote hosts over SSH. It handles OS setup, Python virtualenv creation, source syncing, systemd unit installation, and service management.

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

1. Syncs source code to `install_path`
2. Creates a Python virtualenv at `install_path/.venv`
3. Installs the package with the appropriate service extras
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

1. Syncs updated source code
2. Reinstalls the package (picks up dependency changes)
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

All hosts receive the same base `configs/` directory on every install and upgrade. To give a specific host different settings — a different `room_id`, `audio_device`, LLM model, etc. — create a per-host overlay directory:

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

```yaml
# configs/hosts/living-room/node.yaml  (full copy of configs/node.yaml, with these lines changed)
room_id: "living_room"
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
