import { html, useState, useEffect } from "../html.js";
import { confirmDialog } from "../dialog.js";
import { send, notify } from "../store.js";
import { nodeEnum, nodeHelp, NODE_GROUPS } from "../schema.js";
import { AudioWizard } from "./audio-wizard.js";

// Editor field types per key (the server's allow-list).
const TYPES = {
  wakeword_threshold: "num",
  wakeword_vad_threshold: "num",
  silence_rms_threshold: "num",
  silence_ms: "num",
  speech_min_ms: "num",
  no_speech_timeout_ms: "num",
  hard_cap_ms: "num",
  volume: "range",
  capture_sample_rate: "num",
  playback_sample_rate: "num",
  vad_enabled: "bool",
  wakeword_models: "list",
  audio_device: "str",
  sound_ready: "str",
  sound_waiting: "str",
  sound_connect: "str",
  sound_disconnect: "str",
  sound_ringback: "str",
  // Timer/alarm tones + the failure cue: streamed by the SERVER, so they apply
  // live (deliberately not in RESTART_KEYS, unlike the node-played sounds).
  sound_timer: "str",
  sound_alarm: "str",
  sound_error: "str",
  // Declared hardware capability (can't be detected): false ⇒ half-duplex room —
  // wake ignored during playback; intercom and alarm ring loops disabled.
  hardware_aec: "bool",
  // Dialog-turn tuning (stage 1 conversational flow): all live.
  dialog_no_speech_timeout_ms: "num",
  dialog_onset_ms: "num",
  dialog_onset_vad_threshold: "num",
};

// Keys that re-init audio hardware: a change is pulled on the node's next boot,
// or applied immediately via a Restart. Everything else applies live on save.
const RESTART_KEYS = new Set([
  "audio_device",
  "capture_sample_rate",
  "playback_sample_rate",
  "wakeword_models",
  "wakeword_vad_threshold",
  "sound_ready",
  "sound_waiting",
  "sound_connect",
  "sound_disconnect",
  "sound_ringback",
]);

// Bounds for "range" (slider) fields (default = the node's value when unset).
const RANGES = { volume: { min: 0, max: 100, step: 1, default: 100 } };

// Node-code defaults the SERVER can't see (they live in the node's Python, not
// in node_defaults), so the editor can show the real default as the placeholder
// instead of a bare "default". Keep in sync with the cfg.get() defaults in
// node/client.py. (sound_dialog_end is intentionally absent — it has no default;
// it's off unless set.)
const DEFAULTS = {
  sound_ready: "ready.wav",
  sound_waiting: "waiting.wav",
  sound_connect: "connect.wav",
  sound_disconnect: "disconnect.wav",
  sound_ringback: "ringback.wav",
};

export function ConfigView({ node, onBack }) {
  const [info, setInfo] = useState(null);
  const [over, setOver] = useState({});
  const [room, setRoom] = useState("");
  const [saving, setSaving] = useState(false);
  const [showWizard, setShowWizard] = useState(false);

  async function load() {
    const r = await fetch(`/api/nodes/${encodeURIComponent(node)}/config`);
    const data = await r.json();
    setInfo(data);
    setOver({ ...(data.override || {}) });
    setRoom(data.room || "");
  }
  useEffect(() => {
    load();
  }, [node]);

  if (!info) return html`<div class="empty">Loading…</div>`;

  const setKey = (k, v) => {
    const next = { ...over };
    if (v === undefined) delete next[k];
    else next[k] = v;
    setOver(next);
  };

  async function save(extra = {}) {
    setSaving(true);
    // Drop blank rows from list fields; omit a key entirely if it ends up empty.
    const config = {};
    for (const [key, val] of Object.entries({ ...over, ...extra })) {
      if (key === "room_id") continue; // server-managed, set via the room field
      if (TYPES[key] === "list") {
        const arr = (val || []).map((s) => s.trim()).filter(Boolean);
        if (arr.length) config[key] = arr;
      } else if (TYPES[key] === "num" || TYPES[key] === "range") {
        // Number/range fields hold a raw string while editing (so decimals like
        // "0.5" aren't collapsed mid-type); coerce here, dropping blank/invalid.
        const num = Number(val);
        if (val !== "" && val != null && !Number.isNaN(num)) config[key] = num;
      } else {
        config[key] = val;
      }
    }
    const res = await send("set_override", { node, config });
    setSaving(false);
    if (res.ok) {
      notify(`Config saved — applied live if connected.`);
      load();
    } else {
      notify(res.error || "Save failed.", "err");
    }
  }

  async function saveRoom() {
    const res = await send("set_room", { node, name: room.trim() });
    if (res.ok) {
      notify(`Room name set to “${room.trim()}” — applied now if connected, else on next connect.`);
      load();
    } else {
      notify(res.error || "Could not set the room name.", "err");
    }
  }

  async function ctl(type) {
    const res = await send(type, { node });
    notify(res.ok ? `${cap(type)} sent.` : res.error || `${type} failed`, res.ok ? "ok" : "err");
  }

  async function upgradeNode() {
    if (
      !(await confirmDialog(
        "Upgrade this node to the latest release and restart it? It installs in the " +
          "background; the node disconnects and reconnects on the new version (watch the " +
          "version on its fleet card). Your constraints.txt pins are honored.",
        { title: "Upgrade node", confirmText: "Upgrade" },
      ))
    )
      return;
    const res = await send("upgrade_node", { node });
    notify(
      res.ok ? "Upgrade sent — the node reconnects on the new version." : res.error || "Upgrade failed.",
      res.ok ? "ok" : "err",
    );
  }

  // Guided audio setup/calibration; the raw audio keys stay in the settings grid below.
  function audioSection() {
    return html`
      <div class="audio-row">
        <span class="micro">Guided setup measures your mic and suggests the device + thresholds.</span>
        <button class="btn-ghost" disabled=${!info.controls || !info.connected}
          title=${info.connected ? "" : "Node must be connected"}
          onClick=${() => setShowWizard(true)}>Set up / calibrate audio…</button>
      </div>`;
  }

  async function toggleMute() {
    const next = !info.config.muted;
    const res = await send("set_muted", { node, muted: next });
    if (res.ok) {
      notify(next ? "Muted — the ready chime still plays." : "Unmuted.");
      load();
    } else {
      notify(res.error || "Could not change mute.", "err");
    }
  }

  const row = (k) => {
    const t = TYPES[k] || "str";
    const inherited = info.config[k];
    const effDefault = inherited ?? DEFAULTS[k]; // what's used if left blank
    const cur = over[k];
    const set = cur !== undefined;
    const opts = nodeEnum(k);
    let input;
    if (opts) {
      input = html`<select disabled=${!info.controls}
        onChange=${(e) => setKey(k, e.target.value === "" ? undefined : e.target.value)}>
        <option value="" selected=${!set}>inherit (${fmt(effDefault)})</option>
        ${opts.map((o) => html`<option value=${o} selected=${cur === o}>${o}</option>`)}
      </select>`;
    } else if (t === "bool") {
      input = html`<select disabled=${!info.controls}
        onChange=${(e) => setKey(k, e.target.value === "" ? undefined : e.target.value === "true")}>
        <option value="" selected=${!set}>inherit (${fmt(effDefault)})</option>
        <option value="true" selected=${cur === true}>on</option>
        <option value="false" selected=${cur === false}>off</option>
      </select>`;
    } else if (t === "list") {
      const items = set ? cur : [];
      const update = (next) => setKey(k, next.length ? next : undefined);
      input = html`<div class="list-edit">
        ${items.map(
          (item, i) => html`
            <div class="list-item" key=${i}>
              <input disabled=${!info.controls} value=${item} placeholder="path to model"
                onInput=${(e) => update(items.map((v, j) => (j === i ? e.target.value : v)))} />
              <button class="list-x btn-ghost" disabled=${!info.controls} title="Remove"
                onClick=${() => update(items.filter((_, j) => j !== i))}>×</button>
            </div>
          `,
        )}
        <button class="list-add btn-ghost" disabled=${!info.controls}
          onClick=${() => setKey(k, [...items, ""])}>+ Add model</button>
      </div>`;
    } else if (t === "range") {
      const r = RANGES[k];
      const val = set ? cur : inherited != null ? inherited : (r.default ?? r.min);
      input = html`<div class="range-edit">
        <input type="range" min=${r.min} max=${r.max} step=${r.step} disabled=${!info.controls}
          value=${val} onInput=${(e) => setKey(k, e.target.value)} />
        <span class="range-val mono">${val}</span>
      </div>`;
    } else if (t === "num") {
      // Keep the raw string while typing — coercing to Number() per keystroke turns
      // "0." into 0 and snaps the field back, making decimals impossible to enter.
      input = html`<input type="number" step="any" inputmode="decimal" disabled=${!info.controls}
        value=${set ? cur : ""} placeholder=${effDefault ?? "default"}
        onInput=${(e) => setKey(k, e.target.value === "" ? undefined : e.target.value)} />`;
    } else {
      input = html`<input disabled=${!info.controls} value=${set ? cur : ""}
        placeholder=${effDefault ?? "default"}
        onInput=${(e) => setKey(k, e.target.value === "" ? undefined : e.target.value)} />`;
    }
    const restart = RESTART_KEYS.has(k);
    return html`
      <div class=${"cfg-row" + (set ? " overridden" : "")}>
        <div class="cfg-key"><span class="mono">${k}</span>
          <span class=${"applies " + (restart ? "restart" : "live")}
                title=${restart
                  ? "Audio hardware key — applied on the node's next boot or via Restart"
                  : "Applied live on save"}>${restart ? "restart" : "live"}</span>
          ${nodeHelp(k) ? html`<span class="cfg-help">${nodeHelp(k)}</span>` : null}</div>
        <div class="cfg-input">${input}</div>
      </div>`;
  };

  // VAD-only timing keys are moot when voice-activity detection is off.
  const vadOn = (over.vad_enabled ?? info.config.vad_enabled) !== false;
  const vadGated = new Set([
    "silence_ms",
    "speech_min_ms",
    "no_speech_timeout_ms",
    "hard_cap_ms",
  ]);
  const visible = (k) => (vadGated.has(k) ? vadOn : true);

  // Group the editable keys into logical sections (NODE_GROUPS order); anything
  // not listed falls into "Other".
  const editable = info.editable.filter(visible);
  const grouped = [];
  const seen = new Set();
  for (const [label, ks] of NODE_GROUPS) {
    const present = ks.filter((k) => editable.includes(k));
    present.forEach((k) => seen.add(k));
    if (present.length) grouped.push([label, present]);
  }
  const other = editable.filter((k) => !seen.has(k));
  if (other.length) grouped.push(["Other", other]);
  const groupBlock = ([label, ks]) => html`
    <div class="cfg-group">
      <div class="cfg-group-head mono">${label}</div>
      ${ks.map(row)}
    </div>`;

  return html`
    <div class="cfg">
      <button class="btn-ghost back" onClick=${onBack}>← Fleet</button>
      <div class="section">
        <header><h2>${info.room || "Node"}</h2><span class="rule"></span><a class="docs-link" href="https://docs.kenzy.ai/configuration/node/" target="_blank" rel="noopener">Docs ↗</a></header>
        <p class="micro mono node-sub">node ${info.node_id}${info.connected ? "" : " · offline"}</p>
        ${!info.controls
          ? html`<div class="banner">Editing is read-only — set <code class="mono">dashboard.controls: true</code> in server.yaml to enable.</div>`
          : null}
        <div class="name-row">
          <div class="cfg-key"><span class="mono">room name</span>
            <span class="micro">server-owned; stored & pulled on connect, and sent to the assistant</span></div>
          <div class="name-input">
            <input disabled=${!info.controls} value=${room}
                   placeholder="kitchen" maxlength="64"
                   onInput=${(e) => setRoom(e.target.value)} />
            <button class="btn-ghost" disabled=${!info.controls || !room.trim()}
                    onClick=${saveRoom}>Set</button>
          </div>
        </div>
        ${!info.connected
          ? html`<p class="micro">Node is offline — the room name is saved now and applied when it connects.</p>`
          : null}
        <p class="micro">Badges: <span class="applies live">live</span> applies on save ·
          <span class="applies restart">restart</span> audio keys apply on the node's next boot or via Restart below.</p>
        ${audioSection()}
        <div class="cfg-grid">${grouped.map(groupBlock)}</div>
        <div class="cfg-actions">
          <button class="btn-primary" disabled=${!info.controls || saving} onClick=${() => save()}>
            ${saving ? "Saving…" : "Save & apply"}</button>
        </div>
      </div>

      <div class="section">
        <header><h2>Controls</h2><span class="rule"></span></header>
        <div class="ctl-row">
          <button class="btn-ghost" disabled=${!info.controls} onClick=${() => ctl("trigger")}>Trigger</button>
          <button class="btn-ghost" disabled=${!info.controls} onClick=${() => ctl("stop")}>Stop</button>
          <button class="btn-ghost" disabled=${!info.controls || !info.connected}
                  title=${info.connected ? "" : "Node must be connected"}
                  onClick=${toggleMute}>${info.config.muted ? "Unmute" : "Mute"}</button>
          <button class="btn-ghost" disabled=${!info.controls || !info.connected}
                  title=${info.connected ? "" : "Node must be connected"}
                  onClick=${upgradeNode}>Upgrade</button>
          <button class="btn-ghost danger" disabled=${!info.controls} onClick=${() => ctl("restart")}>Restart</button>
        </div>
        <p class="micro">Volume is in the settings above (0–100, applies live). Mute is temporary — a node comes back un-muted after a restart, and the wake-word chime stays audible while muted.</p>
      </div>

      ${showWizard
        ? html`<${AudioWizard} node=${node} info=${info}
            onApplied=${load} onClose=${() => {
              setShowWizard(false);
              load();
            }} />`
        : null}
    </div>`;
}

function fmt(v) {
  if (v === undefined || v === null) return "default";
  if (Array.isArray(v)) return v.length ? v.join(", ") : "bundled";
  if (typeof v === "boolean") return v ? "on" : "off";
  return String(v);
}

function cap(s) {
  return s.charAt(0).toUpperCase() + s.slice(1);
}
