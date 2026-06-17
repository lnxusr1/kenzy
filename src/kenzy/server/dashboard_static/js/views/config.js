import { html, useState, useEffect } from "../html.js";
import { send, notify } from "../store.js";

// Editor field types per key (the server's allow-list).
const TYPES = {
  wakeword_threshold: "num",
  wakeword_vad_threshold: "num",
  silence_rms_threshold: "num",
  silence_ms: "num",
  speech_min_ms: "num",
  no_speech_timeout_ms: "num",
  hard_cap_ms: "num",
  capture_sample_rate: "num",
  playback_sample_rate: "num",
  vad_enabled: "bool",
  wakeword_models: "list",
  audio_device: "str",
  sound_ready: "str",
  sound_waiting: "str",
};

export function ConfigView({ room, onBack }) {
  const [info, setInfo] = useState(null);
  const [over, setOver] = useState({});
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);

  async function load() {
    const r = await fetch(`/api/rooms/${encodeURIComponent(room)}/config`);
    const data = await r.json();
    setInfo(data);
    setOver({ ...(data.override || {}) });
    setName(data.display_name || "");
  }
  useEffect(() => {
    load();
  }, [room]);

  if (!info) return html`<div class="empty">Loading ${room}…</div>`;

  const setKey = (k, v) => {
    const next = { ...over };
    if (v === undefined) delete next[k];
    else next[k] = v;
    setOver(next);
  };

  async function save() {
    setSaving(true);
    // Drop blank rows from list fields; omit a key entirely if it ends up empty.
    const config = {};
    for (const [key, val] of Object.entries(over)) {
      if (TYPES[key] === "list") {
        const arr = (val || []).map((s) => s.trim()).filter(Boolean);
        if (arr.length) config[key] = arr;
      } else {
        config[key] = val;
      }
    }
    const res = await send("set_override", { room, config });
    setSaving(false);
    if (res.ok) {
      notify(`Config saved for ${room} — applied live if connected.`);
      load();
    } else {
      notify(res.error || "Save failed.", "err");
    }
  }

  async function saveName() {
    const res = await send("set_name", { room, name });
    if (res.ok) {
      notify(name ? `Renamed ${room} → “${name}”.` : `Name cleared for ${room}.`);
      load();
    } else {
      notify(res.error || "Could not save name.", "err");
    }
  }

  async function ctl(type) {
    const res = await send(type, { room });
    notify(res.ok ? `${cap(type)} sent to ${room}.` : res.error || `${type} failed`, res.ok ? "ok" : "err");
  }

  const row = (k) => {
    const t = TYPES[k] || "str";
    const inherited = info.config[k];
    const cur = over[k];
    const set = cur !== undefined;
    let input;
    if (t === "bool") {
      input = html`<select disabled=${!info.controls}
        onChange=${(e) => setKey(k, e.target.value === "" ? undefined : e.target.value === "true")}>
        <option value="" selected=${!set}>inherit</option>
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
    } else if (t === "num") {
      input = html`<input type="number" step="any" disabled=${!info.controls}
        value=${set ? cur : ""} placeholder=${inherited ?? "default"}
        onInput=${(e) => setKey(k, e.target.value === "" ? undefined : Number(e.target.value))} />`;
    } else {
      input = html`<input disabled=${!info.controls} value=${set ? cur : ""}
        placeholder=${inherited ?? "default"}
        onInput=${(e) => setKey(k, e.target.value === "" ? undefined : e.target.value)} />`;
    }
    return html`
      <div class=${"cfg-row" + (set ? " overridden" : "")}>
        <div class="cfg-key"><span class="mono">${k}</span>
          <span class="micro">${set ? "override" : `inherits ${fmt(inherited)}`}</span></div>
        <div class="cfg-input">${input}</div>
      </div>`;
  };

  return html`
    <div class="cfg">
      <button class="btn-ghost back" onClick=${onBack}>← Fleet</button>
      <div class="section">
        <header><h2>Config · <span class="mono">${room}</span></h2><span class="rule"></span></header>
        ${!info.controls
          ? html`<div class="banner">Editing is read-only — set <code class="mono">dashboard.controls: true</code> in server.yaml to enable.</div>`
          : null}
        <div class="name-row">
          <div class="cfg-key"><span class="mono">display name</span>
            <span class="micro">shown in the fleet grid</span></div>
          <div class="name-input">
            <input disabled=${!info.controls} value=${name}
                   placeholder=${room} maxlength="64"
                   onInput=${(e) => setName(e.target.value)} />
            <button class="btn-ghost" disabled=${!info.controls} onClick=${saveName}>Rename</button>
          </div>
        </div>
        <div class="cfg-grid">${info.editable.map(row)}</div>
        <div class="cfg-actions">
          <button class="btn-primary" disabled=${!info.controls || saving} onClick=${save}>
            ${saving ? "Saving…" : "Save & apply"}</button>
        </div>
      </div>

      <div class="section">
        <header><h2>Controls</h2><span class="rule"></span></header>
        <div class="ctl-row">
          <button class="btn-ghost" disabled=${!info.controls} onClick=${() => ctl("trigger")}>Trigger</button>
          <button class="btn-ghost" disabled=${!info.controls} onClick=${() => ctl("stop")}>Stop</button>
          <button class="btn-ghost danger" disabled=${!info.controls} onClick=${() => ctl("restart")}>Restart</button>
        </div>
      </div>
    </div>`;
}

function fmt(v) {
  if (v === undefined || v === null) return "default";
  if (Array.isArray(v)) return v.length ? v.join(", ") : "bundled";
  return String(v);
}

function cap(s) {
  return s.charAt(0).toUpperCase() + s.slice(1);
}
