# Add-ons

Add-ons (5.1) are separately installed pip packages that extend Kenzy — the
first is [kenzy-ld2450](https://github.com/lnxusr1/kenzy-ld2450), the in-node
room radar. **An add-on you haven't installed has no configuration at all**:
no keys are offered, nothing below applies, and nothing needs cleaning up.

This page documents the *mechanism* — where add-on settings live and how they
merge. Each add-on's actual keys are documented by the add-on itself (its
README and its dashboard panel).

## Where add-on settings live

Add-ons follow the same rule as everything else: **the server owns the
configuration**, and each add-on's settings are kept apart from everyone
else's so no two surfaces can overwrite each other.

### Server-side settings: `configs/addons/<id>.yaml`

An add-on's server half reads its settings from its **own file** in the
config home, deep-merged over the defaults the add-on declares. For example,
`configs/addons/ld2450.yaml`:

```yaml
# kenzy-ld2450's server half
stale_after_s: 15
```

Absent file ⇒ the add-on's defaults apply. These are read when the server
starts (install/uninstall of add-ons is also restart-to-apply — the
dashboard's Settings → Add-ons card says so).

### Per-node settings: the `addons:` namespace

An add-on's node half is configured like any other node setting — through the
server, under an `addons:` block keyed by add-on id. Fleet-wide defaults go
in `server.yaml`'s `node_defaults`; per-room values go in the node's override
(`configs/nodes/<node_id>.yaml`), which is what the add-on's dashboard panel
writes:

```yaml
# configs/nodes/<node_id>.yaml
addons:
  ld2450:
    device: /dev/serial/by-id/usb-Silicon_Labs_CP2102_...-port0
    ignore_zones:
      - [-2200, 2200, -1200, 3200]
```

Two properties worth knowing:

- **The namespace merges per-add-on, never wholesale.** A per-node override
  touching one add-on's key keeps the fleet defaults' *other* add-ons and
  that add-on's sibling keys intact. Editing one add-on from its panel can't
  disturb another's settings.
- **Changes apply live.** Saving from an add-on's panel pushes the node's
  config immediately; the add-on's node half restarts itself with the new
  values — no node restart.

## Naming rule for add-on keys

Key names matching the server's secret filter (`key`, `token`, `secret`,
`password`, `credential` as substrings) are **refused at save time** — the
served config strips such names to keep real secrets off nodes, so a setting
named like one could never arrive. The dashboard tells you to rename it
rather than silently serving a config with the key missing.

## When an add-on can't load

Version pairing and load failures are surfaced, never silent: the Settings →
Add-ons card lists every installed add-on — including ones that *couldn't*
load, with the reason and the fix — and an amber "not loaded" marker appears
in the navigation. Kenzy-driven upgrades move Kenzy and installed add-ons
together as one set, so an upgrade can't strand an incompatible pairing; see
the [changelog](https://github.com/lnxusr1/kenzy/blob/main/CHANGELOG.md) for
5.1.0's details.
