import { html, useState, useEffect } from "../html.js";
import { useFleet, send, notify } from "../store.js";
import { getSettings } from "../api.js";

// --- nested ⇄ flat (dotted path) helpers for the generic editor -------------

function flatten(obj, prefix = "") {
  const out = {};
  for (const [k, v] of Object.entries(obj || {})) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === "object" && !Array.isArray(v)) Object.assign(out, flatten(v, key));
    else out[key] = v;
  }
  return out;
}

function setPath(obj, path, value) {
  const parts = path.split(".");
  let cur = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    if (!cur[parts[i]] || typeof cur[parts[i]] !== "object") cur[parts[i]] = {};
    cur = cur[parts[i]];
  }
  cur[parts[parts.length - 1]] = value;
}

const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const typeOf = (v) => (typeof v === "boolean" ? "bool" : typeof v === "number" ? "num" : "str");

function ServiceEditor({ name, onBack }) {
  const [info, setInfo] = useState(null);
  const [vals, setVals] = useState({}); // flat dotted → edited value
  const [orig, setOrig] = useState({}); // flat dotted → effective (baseline)
  const [saving, setSaving] = useState(false);

  async function load() {
    const r = await fetch(`/api/services/${encodeURIComponent(name)}/config`);
    if (!r.ok) {
      setInfo({ error: true });
      return;
    }
    const data = await r.json();
    const flat = flatten(data.config);
    setInfo(data);
    setOrig(flat);
    setVals({ ...flat });
  }
  useEffect(() => {
    load();
  }, [name]);

  if (!info) return html`<div class="empty">Loading…</div>`;
  if (info.error) return html`<div class="empty">Could not load ${name} config.</div>`;

  const ovFlat = flatten(info.override);
  const setKey = (k, v) => setVals({ ...vals, [k]: v });

  async function save() {
    // Start from the stored override (so untouched overrides are preserved) and
    // apply every leaf the user changed away from the effective baseline.
    const override = JSON.parse(JSON.stringify(info.override || {}));
    for (const [k, v] of Object.entries(vals)) {
      if (!eq(v, orig[k])) setPath(override, k, v);
    }
    setSaving(true);
    const res = await send("set_service_config", { service: name, config: override });
    setSaving(false);
    if (res.ok) {
      notify(`${name} config saved${info.reachable ? " — service restarting" : ""}.`);
      load();
    } else {
      notify(res.error || "Save failed.", "err");
    }
  }

  async function restart() {
    const res = await send("restart_service", { service: name });
    notify(res.ok ? `${name} restarting.` : res.error || "Restart failed.", res.ok ? "ok" : "err");
  }

  const keys = Object.keys(orig).sort();
  const row = (k) => {
    const t = typeOf(orig[k]);
    const v = vals[k];
    const overridden = k in ovFlat;
    let input;
    if (t === "bool") {
      input = html`<select disabled=${!info.controls}
        onChange=${(e) => setKey(k, e.target.value === "true")}>
        <option value="true" selected=${v === true}>on</option>
        <option value="false" selected=${v === false}>off</option>
      </select>`;
    } else if (t === "num") {
      input = html`<input type="number" step="any" disabled=${!info.controls}
        value=${v ?? ""} onInput=${(e) => setKey(k, e.target.value === "" ? null : Number(e.target.value))} />`;
    } else {
      input = html`<input disabled=${!info.controls} value=${v ?? ""}
        placeholder=${orig[k] === null ? "null" : ""}
        onInput=${(e) => setKey(k, e.target.value)} />`;
    }
    return html`
      <div class=${"cfg-row" + (overridden ? " overridden" : "")}>
        <div class="cfg-key"><span class="mono">${k}</span>
          <span class="micro">${overridden ? "override" : "default"}</span></div>
        <div class="cfg-input">${input}</div>
      </div>`;
  };

  return html`
    <div class="cfg">
      <button class="btn-ghost back" onClick=${onBack}>← Services</button>
      <div class="section">
        <header><h2>${name}</h2><span class="rule"></span></header>
        ${!info.controls
          ? html`<div class="banner">Editing is read-only — set <code class="mono">dashboard.controls: true</code> in server.yaml to enable.</div>`
          : null}
        <p class="micro">Saved to <span class="mono">configs/services/${name}.yaml</span> on the server;
          the service restarts to apply. Secrets are read from the service host's environment and are never shown or stored here.
          ${info.reachable ? "" : " This service has no configured URL, so it can't be auto-restarted — restart it manually."}</p>
        <div class="cfg-grid">${keys.map(row)}</div>
        <div class="cfg-actions">
          <button class="btn-primary" disabled=${!info.controls || saving} onClick=${save}>
            ${saving ? "Saving…" : "Save & restart"}</button>
        </div>
      </div>

      <div class="section">
        <header><h2>Controls</h2><span class="rule"></span></header>
        <div class="ctl-row">
          <button class="btn-ghost danger" disabled=${!info.controls || !info.reachable}
            onClick=${restart}>Restart</button>
        </div>
      </div>
    </div>`;
}

// Controlled by the shell so a service can be deep-linked (e.g. from a Fleet
// chip): `selected` is the open service or null for the list; `onSelect` changes it.
export function ServicesView({ selected = null, onSelect }) {
  const { data } = useFleet();
  const [list, setList] = useState(null);

  useEffect(() => {
    getSettings()
      .then((s) => setList(s.services || []))
      .catch(() => setList([]));
  }, []);

  if (selected) return html`<${ServiceEditor} name=${selected} onBack=${() => onSelect(null)} />`;
  if (!list) return html`<div class="empty">Loading…</div>`;
  if (!list.length)
    return html`<div class="empty">No backend services configured (set their <span class="mono">url</span> in server.yaml).</div>`;

  const health = Object.fromEntries(((data && data.services) || []).map((s) => [s.name, s.up]));

  return html`
    <div class="chips">
      ${list.map((svc) => {
        const up = health[svc.name];
        return html`
          <div key=${svc.name} class="chip" onClick=${() => onSelect(svc.name)}
               role="button" tabindex="0">
            <span class=${"led " + (up ? "up" : "down")}></span>
            <span class="name">${svc.name}</span>
            <span class="detail">${svc.url}</span>
          </div>`;
      })}
    </div>`;
}
