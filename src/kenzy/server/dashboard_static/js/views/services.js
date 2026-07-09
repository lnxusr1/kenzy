import { html, useState, useEffect } from "../html.js";
import { useFleet, send, notify } from "../store.js";
import { getSettings } from "../api.js";
import { serviceEnum, serviceHelp, groupByParent, groupBySections, SERVICE_SECTIONS, fieldVisible } from "../schema.js";

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
const typeOf = (v) =>
  Array.isArray(v)
    ? "list"
    : typeof v === "boolean"
      ? "bool"
      : typeof v === "number"
        ? "num"
        : "str";

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
      let val = v;
      // Number fields hold a raw string while editing; coerce back to a number so
      // decimals (e.g. 0.25) survive and aren't written to YAML as strings.
      if (typeof orig[k] === "number" && typeof v === "string") {
        if (v === "") continue;
        const num = Number(v);
        if (Number.isNaN(num)) continue;
        val = num;
      } else if (Array.isArray(v)) {
        // Drop blank rows; trim string items.
        val = v.map((s) => (typeof s === "string" ? s.trim() : s)).filter((s) => s !== "" && s != null);
      }
      if (!eq(val, orig[k])) setPath(override, k, val);
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

  async function upgrade() {
    if (
      !window.confirm(
        `Upgrade ${name} to the latest release and restart it? It installs in the ` +
          `background (a few minutes) and reports the result; your constraints.txt pins ` +
          `are honored.`,
      )
    )
      return;
    const res = await send("upgrade_service", { service: name });
    notify(
      res.ok ? `${name} upgrade started — watch for the result.` : res.error || "Upgrade failed.",
      res.ok ? "ok" : "err",
    );
  }

  // Only render fields whose dependency (e.g. provider) is currently satisfied,
  // then group them by parent path so related settings sit together.
  const visibleKeys = Object.keys(orig)
    .filter((k) => fieldVisible(name, k, vals))
    .sort();
  const groups = SERVICE_SECTIONS[name]
    ? groupBySections(visibleKeys, SERVICE_SECTIONS[name])
    : groupByParent(visibleKeys);
  // Show just the leaf when the group header already gives the parent (e.g.
  // "model" under the "whisper" group); show the full dotted path otherwise (a
  // "tts.url" peer key in a semantic group would be a context-less "url").
  const label = (k, group) =>
    k.includes(".") && k.slice(0, k.lastIndexOf(".")) === group ? k.slice(k.lastIndexOf(".") + 1) : k;
  const row = (k, group) => {
    const t = typeOf(orig[k]);
    const v = vals[k];
    const overridden = k in ovFlat;
    const opts = serviceEnum(name, k);
    const help = serviceHelp(name, k);
    let input;
    if (opts) {
      input = html`<select disabled=${!info.controls} onChange=${(e) => setKey(k, e.target.value)}>
        ${opts.map((o) => html`<option value=${o} selected=${v === o}>${o || "(unset)"}</option>`)}
      </select>`;
    } else if (t === "bool" || orig[k] === null) {
      // Booleans render as an on/off chooser; a null-valued key gets a "default"
      // option too (consistent with the node editor's inherit state).
      const nullable = orig[k] === null;
      input = html`<select disabled=${!info.controls}
        onChange=${(e) =>
          setKey(k, e.target.value === "" ? null : e.target.value === "true")}>
        ${nullable ? html`<option value="" selected=${v == null}>default</option>` : null}
        <option value="true" selected=${v === true}>on</option>
        <option value="false" selected=${v === false}>off</option>
      </select>`;
    } else if (t === "num") {
      // Store the raw string while typing (coerced back to a number on save) so a
      // decimal like 0.25 isn't collapsed to an integer mid-keystroke.
      const step = Number.isInteger(orig[k]) ? "1" : "any";
      input = html`<input type="number" step=${step} inputmode="decimal" disabled=${!info.controls}
        value=${v ?? ""} onInput=${(e) => setKey(k, e.target.value)} />`;
    } else if (t === "list") {
      const items = Array.isArray(v) ? v : [];
      const update = (next) => setKey(k, next);
      input = html`<div class="list-edit">
        ${items.map(
          (item, i) => html`
            <div class="list-item" key=${i}>
              <input disabled=${!info.controls} value=${item}
                onInput=${(e) => update(items.map((x, j) => (j === i ? e.target.value : x)))} />
              <button class="list-x btn-ghost" disabled=${!info.controls} title="Remove"
                onClick=${() => update(items.filter((_, j) => j !== i))}>×</button>
            </div>
          `,
        )}
        <button class="list-add btn-ghost" disabled=${!info.controls}
          onClick=${() => update([...items, ""])}>+ Add</button>
      </div>`;
    } else {
      input = html`<input disabled=${!info.controls} value=${v ?? ""}
        placeholder=${orig[k] === null ? "null" : ""}
        onInput=${(e) => setKey(k, e.target.value)} />`;
    }
    return html`
      <div class=${"cfg-row" + (overridden ? " overridden" : "")}>
        <div class="cfg-key"><span class="mono" title=${k}>${label(k, group)}</span>
          ${overridden ? html`<span class="micro">override</span>` : null}
          ${help ? html`<span class="cfg-help">${help}</span>` : null}</div>
        <div class="cfg-input">${input}</div>
      </div>`;
  };

  const groupBlock = ([g, ks]) => html`
    <div class="cfg-group">
      ${g === "General" ? null : html`<div class="cfg-group-head mono">${g}</div>`}
      ${ks.map((k) => row(k, g))}
    </div>`;

  return html`
    <div class="cfg">
      <button class="btn-ghost back" onClick=${onBack}>← Services</button>
      <div class="section">
        <header><h2>${name}</h2><span class="rule"></span><a class="docs-link" href=${`https://docs.kenzy.ai/configuration/${name}/`} target="_blank" rel="noopener">Docs ↗</a></header>
        ${!info.controls
          ? html`<div class="banner">Editing is read-only — set <code class="mono">dashboard.controls: true</code> in server.yaml to enable.</div>`
          : null}
        <p class="micro">Saved to <span class="mono">configs/services/${name}.yaml</span> on the server;
          the service restarts to apply. Secrets are read from the service host's environment and are never shown or stored here.
          ${info.reachable ? "" : " This service has no configured URL, so it can't be auto-restarted — restart it manually."}</p>
        <div class="cfg-grid">${groups.map(groupBlock)}</div>
        <div class="cfg-actions">
          <button class="btn-primary" disabled=${!info.controls || saving} onClick=${save}>
            ${saving ? "Saving…" : "Save & restart"}</button>
        </div>
      </div>

      <div class="section">
        <header><h2>Controls</h2><span class="rule"></span></header>
        <div class="ctl-row">
          <button class="btn-ghost" disabled=${!info.controls || !info.reachable}
            onClick=${upgrade}>Upgrade</button>
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
          <div key=${svc.name} class="chip svc-chip" onClick=${() => onSelect(svc.name)}
               role="button" tabindex="0">
            <span class=${"led " + (up ? "up" : "down")}></span>
            <div class="svc-meta">
              <span class="name">${svc.name}</span>
              <span class="detail" title=${svc.url}>${svc.url}</span>
            </div>
          </div>`;
      })}
    </div>`;
}
