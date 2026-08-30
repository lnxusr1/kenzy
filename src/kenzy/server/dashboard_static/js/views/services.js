import { html, useState, useEffect } from "../html.js";
import { confirmDialog } from "../dialog.js";
import { send, notify } from "../store.js";
import { serviceEnum, serviceHelp, enumLabel, groupByParent, groupBySections, SERVICE_SECTIONS, fieldVisible } from "../schema.js";

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

function delPath(obj, path) {
  // Delete a dotted path, pruning parents that become empty (keeps YAML minimal).
  const parts = path.split(".");
  const chain = [obj];
  for (let i = 0; i < parts.length - 1; i++) {
    const nxt = chain[chain.length - 1][parts[i]];
    if (!nxt || typeof nxt !== "object") return;
    chain.push(nxt);
  }
  delete chain[chain.length - 1][parts[parts.length - 1]];
  for (let i = chain.length - 1; i > 0; i--) {
    if (Object.keys(chain[i]).length === 0) delete chain[i - 1][parts[i - 1]];
  }
}

const typeOf = (v) =>
  Array.isArray(v)
    ? "list"
    : typeof v === "boolean"
      ? "bool"
      : typeof v === "number"
        ? "num"
        : "str";

// Inherited value → human placeholder/label text.
// Inherited/default values → the label shown in an "inherit (…)" option.
//
// An empty string is a REAL value for several settings ("don't send this
// parameter at all", "use the service model") rather than an absent one, so it
// needs a word: without one the label rendered as "inherit ()", which looks
// like a bug and says nothing. "blank" matches the wording the per-field help
// already uses ("Blank = don't send it").
const fmt = (v) => {
  if (v === undefined) return "unset";
  if (v === "") return "blank";
  if (v === null) return "null";
  if (Array.isArray(v)) return v.length ? v.join(", ") : "empty";
  if (typeof v === "boolean") return v ? "on" : "off";
  return String(v);
};

// Text-input placeholders only: a blank/null/absent default shows a BLANK
// placeholder — "blank"/"null"/"unset" written into the field reads like a
// value someone set (founder call 2026-08-28); the help line under the field
// says what blank means. Select options keep fmt(): an option needs text.
const ph = (v) => (v === "" || v == null ? "" : fmt(v));

// Sentinel for the "inherit" select option ("" can be a real enum value, e.g.
// reasoning_effort's "don't send").
const INHERIT = "__inherit__";

function ServiceEditor({ name, onBack }) {
  const [info, setInfo] = useState(null);
  // Node-editor convention: `vals` holds ONLY override-layer values (undefined =
  // inherit); `defs` is the inherited layer (packaged default + auto-wired peers),
  // shown as placeholders. Clearing a field removes the key from the override.
  const [vals, setVals] = useState({});
  const [defs, setDefs] = useState({});
  const [saving, setSaving] = useState(false);
  const [feats, setFeats] = useState(null); // GET /api/services/<name>/features
  const [installing, setInstalling] = useState(false);

  const [unit, setUnit] = useState(null);

  async function loadUnit() {
    try {
      const r = await fetch(`/api/services/${encodeURIComponent(name)}/unit`);
      setUnit(r.ok ? await r.json() : null);
    } catch (e) {
      setUnit(null);
    }
  }

  async function loadFeatures() {
    try {
      const r = await fetch(`/api/services/${encodeURIComponent(name)}/features`);
      setFeats(r.ok ? await r.json() : null);
    } catch (e) {
      setFeats(null);
    }
  }

  async function load() {
    const r = await fetch(`/api/services/${encodeURIComponent(name)}/config`);
    if (!r.ok) {
      setInfo({ error: true });
      return;
    }
    const data = await r.json();
    setInfo(data);
    setDefs(flatten(data.defaults || data.config));
    setVals({ ...flatten(data.override) });
  }
  useEffect(() => {
    load();
    loadFeatures();
    loadUnit();
  }, [name]);

  if (!info) return html`<div class="empty">Loading…</div>`;
  if (info.error) return html`<div class="empty">Could not load ${name} config.</div>`;

  const ovFlat = flatten(info.override);
  const setKey = (k, v) => setVals({ ...vals, [k]: v });
  // Effective view (inherited ← current edits) for dependency-driven visibility.
  const effective = { ...defs };
  for (const [k, v] of Object.entries(vals)) if (v !== undefined) effective[k] = v;

  async function save() {
    // Start from the stored override (preserves keys hidden by a dependency, e.g.
    // openai.* while provider=whisper); every rendered key is then set or removed —
    // an emptied field deletes its key, reverting to the inherited default.
    const override = JSON.parse(JSON.stringify(info.override || {}));
    for (const k of visibleKeys) {
      let val = vals[k];
      // Number fields hold a raw string while editing; coerce back to a number so
      // decimals (e.g. 0.25) survive and aren't written to YAML as strings.
      if (typeof val === "string" && typeOf(baseVal(k)) === "num") {
        const num = Number(val);
        val = val === "" || Number.isNaN(num) ? undefined : num;
      } else if (typeof val === "string" && val === "") {
        val = undefined; // cleared text field = unset
      } else if (Array.isArray(val)) {
        // Drop blank rows; trim string items; empty list = unset.
        val = val.map((s) => (typeof s === "string" ? s.trim() : s)).filter((s) => s !== "" && s != null);
        if (!val.length) val = undefined;
      }
      if (val === undefined) delPath(override, k);
      else setPath(override, k, val);
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
      !(await confirmDialog(
        `Upgrade ${name} to the latest release and restart it? It installs in the ` +
          `background (a few minutes) and reports the result; your constraints.txt pins ` +
          `are honored.`,
        { title: "Upgrade service", confirmText: "Upgrade" },
      ))
    )
      return;
    const res = await send("upgrade_service", { service: name });
    notify(
      res.ok ? `${name} upgrade started — watch for the result.` : res.error || "Upgrade failed.",
      res.ok ? "ok" : "err",
    );
  }

  // Type/inheritance baseline: the inherited layer, falling back to the stored
  // override for custom keys that have no packaged default.
  const baseVal = (k) => (defs[k] !== undefined ? defs[k] : ovFlat[k]);

  // Only render fields whose dependency (e.g. provider) is currently satisfied,
  // then group them by parent path so related settings sit together.
  const visibleKeys = [...new Set([...Object.keys(defs), ...Object.keys(ovFlat)])]
    .filter((k) => fieldVisible(name, k, effective))
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
    const t = typeOf(baseVal(k));
    const v = vals[k];
    const set = v !== undefined;
    const opts = serviceEnum(name, k);
    const help = serviceHelp(name, k);
    let input;
    if (opts) {
      input = html`<select disabled=${!info.controls}
        onChange=${(e) => setKey(k, e.target.value === INHERIT ? undefined : e.target.value)}>
        <option value=${INHERIT} selected=${!set}>inherit (${fmt(defs[k])})</option>
        ${opts.map((o) => html`<option value=${o} selected=${set && v === o}>${enumLabel(name, k, o) || "(blank)"}</option>`)}
      </select>`;
    } else if (t === "bool") {
      input = html`<select disabled=${!info.controls}
        onChange=${(e) => setKey(k, e.target.value === INHERIT ? undefined : e.target.value === "true")}>
        <option value=${INHERIT} selected=${!set}>inherit (${fmt(defs[k])})</option>
        <option value="true" selected=${v === true}>on</option>
        <option value="false" selected=${v === false}>off</option>
      </select>`;
    } else if (t === "num") {
      // Store the raw string while typing (coerced back to a number on save) so a
      // decimal like 0.25 isn't collapsed to an integer mid-keystroke.
      const step = Number.isInteger(baseVal(k)) ? "1" : "any";
      input = html`<input type="number" step=${step} inputmode="decimal" disabled=${!info.controls}
        value=${set ? v : ""} placeholder=${ph(defs[k])}
        onInput=${(e) => setKey(k, e.target.value === "" ? undefined : e.target.value)} />`;
    } else if (t === "list") {
      const items = Array.isArray(v) ? v : [];
      const update = (next) => setKey(k, next.length ? next : undefined);
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
        ${!items.length && Array.isArray(defs[k]) && defs[k].length
          ? html`<span class="micro">inherits: ${defs[k].join(", ")}</span>`
          : null}
        <button class="list-add btn-ghost" disabled=${!info.controls}
          onClick=${() => setKey(k, [...items, ""])}>+ Add</button>
      </div>`;
    } else {
      input = html`<input disabled=${!info.controls} value=${set ? v : ""}
        placeholder=${ph(defs[k])}
        onInput=${(e) => setKey(k, e.target.value === "" ? undefined : e.target.value)} />`;
    }
    return html`
      <div class=${"cfg-row" + (set ? " overridden" : "")}>
        <div class="cfg-key"><span class="mono" title=${k}>${label(k, group)}</span>
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
      <button class="btn-ghost back" onClick=${onBack}>← Fleet</button>
      <div class="section">
        <header><h2>${name}</h2><span class="rule"></span><a class="docs-link" href=${`https://docs.kenzy.ai/configuration/${name}/`} target="_blank" rel="noopener">Docs ↗</a></header>
        ${!info.controls
          ? html`<div class="banner">Editing is read-only — set <code class="mono">dashboard.controls: true</code> in server.yaml to enable.</div>`
          : null}
        <p class="micro">Saved to <span class="mono">configs/services/${name}.yaml</span> on the server;
          the service restarts to apply. Secrets are read from the service host's environment and are never shown or stored here.
          ${info.reachable ? "" : " This service has no configured URL, so it can't be auto-restarted — restart it manually."}</p>
        ${feats && feats.reachable && (feats.features || []).length
          ? html`<div class="feat-chips">
              ${feats.features.map((f) => {
                const state = !f.available
                  ? "missing"
                  : f.active
                    ? "active"
                    : f.configured
                      ? "inactive"
                      : "off";
                const label =
                  state === "missing"
                    ? f.configured
                      ? "enabled in config — NOT INSTALLED"
                      : "not installed"
                    : state === "active"
                      ? "active"
                      : state === "inactive"
                        ? "installed — needs restart or config"
                        : "available, not enabled";
                return html`<div key=${f.name} class=${"chip feat " + state} title=${f.note || ""}>
                  <span class="mono">${f.name}</span>
                  <span class="micro">${label}</span>
                  ${state === "missing" && f.install === "pip" && info.controls
                    ? html`<button class="btn-ghost" disabled=${installing}
                        onClick=${async () => {
                          setInstalling(true);
                          notify(`Installing ${f.name} dependencies — the service restarts itself when done.`);
                          const r = await send("install_feature_deps", { service: name }, 620000);
                          setInstalling(false);
                          notify(r.ok ? "Installed — service restarting." : r.error || "Install failed.", r.ok ? "ok" : "err");
                          if (r.ok) setTimeout(loadFeatures, 6000);
                        }}>${installing ? "Installing…" : "Install"}</button>`
                    : null}
                  ${state === "missing" && f.install === "apt"
                    ? html`<span class="micro mono">${f.note}</span>`
                    : null}
                </div>`;
              })}
            </div>`
          : null}
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
          ${unit && unit.systemd && unit.exists
            ? html`<button class="btn-ghost danger" disabled=${!info.controls}
                onClick=${async () => {
                  const on = !(unit.enabled && unit.active);
                  const verb = on ? "Enable and start" : "Disable and stop";
                  if (!(await confirmDialog(
                    `${verb} ${name}? ${on ? "" : "It stays off until re-enabled (dashboard or systemctl)."}`,
                    { title: on ? "Enable service" : "Disable service", confirmText: verb.split(" ")[0], danger: !on },
                  )))
                    return;
                  const r = await send("set_service_enabled", { service: name, enabled: on }, 30000);
                  notify(r.ok ? `${name} ${on ? "enabled" : "disabling"}.` : r.error || "Failed.", r.ok ? "ok" : "err");
                  setTimeout(loadUnit, 4000);
                }}>${unit.enabled && unit.active ? "Disable service" : "Enable service"}</button>`
            : null}
        </div>
        ${unit && unit.systemd && unit.exists && !unit.active
          ? html`<p class="micro">This service's unit is ${unit.enabled ? "enabled but not running" : "disabled"} —
              if it runs on another host, use
              ${" "}<span class="mono">systemctl --user enable --now ${unit.unit}</span> there.</p>`
          : null}
      </div>
    </div>`;
}

// The service editor, deep-linked from a Fleet chip — there is no standalone
// Services tab anymore (the Fleet view's service chips ARE the list). `selected`
// is the open service; `onSelect(null)` hands the shell back to the fleet.
export function ServicesView({ selected = null, onSelect }) {
  if (!selected) return html`<div class="empty">Pick a service from the fleet.</div>`;
  return html`<${ServiceEditor} name=${selected} onBack=${() => onSelect(null)} />`;
}
