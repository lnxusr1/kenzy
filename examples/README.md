# Example configs

Generic, copy-me templates for the operator config that is **not** committed (your
`configs/nodes/`, `configs/services/`, `configs/server.yaml`, … are gitignored
because they hold site-specific details — room names, your Home Assistant URL, your
location, auth). The full annotated defaults for every key ship inside the package
(`src/kenzy/data/configs/`); these examples show the **central-store override**
layout used by the dashboard and `kenzy-deploy`.

## Layout

```
examples/
  node.yaml                  # shared node bootstrap (how nodes reach the server)
  nodes/living-room.yaml     # per-node override, keyed by node_id (room, audio, tuning)
  services/stt.yaml          # per-service overrides (only the keys you change)
  services/tts.yaml
  services/llm.yaml
  services/speaker.yaml
  server.yaml                # optional server config (binds, backend URLs, dashboard)
```

## Use

Copy what you need into `configs/`, renaming node files to your node_id:

```bash
cp examples/node.yaml                configs/node.yaml
cp examples/nodes/living-room.yaml   configs/nodes/<your-node-id>.yaml
cp examples/services/tts.yaml        configs/services/tts.yaml
```

Then edit the values. With `kenzy-deploy` these are seeded to the server
(seed-don't-clobber) and become editable from the dashboard; you only need an
override file for keys that differ from the packaged defaults. Omit a file entirely
to use the defaults for that service/node.
