import { html, useState, useEffect } from "../html.js";
import { getHaCuration } from "../api.js";
import { send, notify } from "../store.js";

// Home Assistant curation editor. The device topology is pulled live from HA
// (via kenzy-llm); this tab edits the small curation layer HA can't store —
// spoken aliases, context notes, room group-defaults, and voice exclusions.
// Reads /api/ha/curation, saves via the set_ha_curation mutation (controls-gated).

const D2T = { light: "lights", switch: "lights", fan: "fans", cover: "covers", lock: "lock", climate: "climate" };
const slug = (s) => (s || "").toLowerCase().replace(/[^\w\s]/g, "").trim().replace(/\s+/g, "_");
const splitCsv = (s) => s.split(",").map((x) => x.trim()).filter(Boolean);
const splitLines = (s) => s.split("\n").map((x) => x.trim()).filter(Boolean);

function emptyRow() {
  return { aliases: "", note: "", inGroup: true, exclude: false };
}

export function HaView() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [dev, setDev] = useState({}); // entity_id -> {aliases, note, inGroup, exclude}
  const [defs, setDefs] = useState({}); // entity_id -> bool (in its room's default set)
  const [ex, setEx] = useState({ patterns: "", domains: "", areas: "" });
  const [dirty, setDirty] = useState(false);

  function hydrate(d) {
    const cur = d.curation || {};
    const devs = {};
    for (const [id, dc] of Object.entries(cur.devices || {})) {
      devs[id] = {
        aliases: (dc.aliases || []).join(", "),
        note: dc.note || "",
        inGroup: dc.in_group !== false,
        exclude: dc.hidden === true,
      };
    }
    for (const id of (cur.exclude && cur.exclude.entities) || []) {
      devs[id] = devs[id] || emptyRow();
      devs[id].exclude = true;
    }
    const defset = {};
    for (const rv of Object.values(cur.rooms || {})) {
      for (const ids of Object.values((rv && rv.defaults) || {})) {
        for (const id of ids) defset[id] = true;
      }
    }
    setDev(devs);
    setDefs(defset);
    setEx({
      patterns: ((cur.exclude && cur.exclude.patterns) || []).join("\n"),
      domains: ((cur.exclude && cur.exclude.domains) || []).join(", "),
      areas: ((cur.exclude && cur.exclude.areas) || []).join(", "),
    });
    setDirty(false);
  }

  const load = () =>
    getHaCuration()
      .then((d) => {
        setData(d);
        hydrate(d);
      })
      .catch((e) => setErr(String(e)));

  useEffect(() => {
    load();
  }, []);

  const rowOf = (id) => dev[id] || emptyRow();
  const setRow = (id, patch) => {
    setDev((m) => ({ ...m, [id]: { ...rowOf(id), ...patch } }));
    setDirty(true);
  };
  const setDefault = (id, on) => {
    setDefs((m) => ({ ...m, [id]: on }));
    setDirty(true);
  };
  const setExField = (k, v) => {
    setEx((m) => ({ ...m, [k]: v }));
    setDirty(true);
  };

  function build() {
    const byId = {};
    for (const e of data.devices || []) byId[e.entity_id] = e;

    const out = {};
    const exclude = {};
    const exEntities = Object.entries(dev).filter(([, v]) => v.exclude).map(([id]) => id);
    if (exEntities.length) exclude.entities = exEntities.sort();
    if (splitLines(ex.patterns).length) exclude.patterns = splitLines(ex.patterns);
    if (splitCsv(ex.domains).length) exclude.domains = splitCsv(ex.domains);
    if (splitCsv(ex.areas).length) exclude.areas = splitCsv(ex.areas);
    if (Object.keys(exclude).length) out.exclude = exclude;

    const dmap = {};
    for (const [id, v] of Object.entries(dev)) {
      if (v.exclude) continue;
      const cd = {};
      const al = splitCsv(v.aliases);
      if (al.length) cd.aliases = al;
      if (v.note.trim()) cd.note = v.note.trim();
      if (!v.inGroup) cd.in_group = false;
      if (Object.keys(cd).length) dmap[id] = cd;
    }
    if (Object.keys(dmap).length) out.devices = dmap;

    const rooms = {};
    for (const [id, on] of Object.entries(defs)) {
      const e = byId[id];
      if (!on || !e) continue;
      const area = slug(e.area_name);
      const type = D2T[e.domain] || e.domain;
      rooms[area] = rooms[area] || { defaults: {} };
      (rooms[area].defaults[type] = rooms[area].defaults[type] || []).push(id);
    }
    if (Object.keys(rooms).length) out.rooms = rooms;

    return out;
  }

  async function save() {
    setBusy(true);
    const res = await send("set_ha_curation", { curation: build() });
    if (res.ok) {
      notify("Curation saved.");
      await load();
    } else {
      notify(res.error || "Could not save curation.", "err");
    }
    setBusy(false);
  }

  if (err) return html`<div class="empty">Could not load curation: ${err}</div>`;
  if (!data) return html`<div class="empty">Loading…</div>`;
  if (!data.reachable)
    return html`<div class="empty">The LLM service isn't reachable, so Home Assistant curation
      can't be edited. Check <span class="mono">llm.url</span> and that
      <span class="mono">kenzy-llm</span> is running.</div>`;

  const controls = data.controls;
  const devices = data.devices || [];
  const ro = !controls;

  // Group floor -> area -> domain for the tree.
  const tree = {};
  for (const e of devices) {
    const f = (tree[e.floor_name] = tree[e.floor_name] || {});
    const a = (f[e.area_name] = f[e.area_name] || {});
    (a[e.domain] = a[e.domain] || []).push(e);
  }

  const included = devices.filter((e) => e.included).length;
  const aliasCount = Object.values(dev).filter((v) => v.aliases.trim()).length;

  const checkbox = (label, checked, onChange) => html`
    <label class="ha-chk"><input type="checkbox" disabled=${ro} checked=${checked}
      onChange=${(e) => onChange(e.target.checked)} /> ${label}</label>`;

  const row = (e) => {
    const r = rowOf(e.entity_id);
    return html`
      <div class=${"ha-row" + (r.exclude ? " excluded" : "")} key=${e.entity_id}>
        <div class="ha-ent">
          <span class="mono">${e.entity_id}</span>
          <span class="micro">${e.name}${e.included ? "" : ` · excluded (${e.reason})`}</span>
        </div>
        <input class="ha-in" placeholder="aliases (comma-separated)" disabled=${ro}
          value=${r.aliases} onInput=${(ev) => setRow(e.entity_id, { aliases: ev.target.value })} />
        <input class="ha-in" placeholder="note / context" disabled=${ro}
          value=${r.note} onInput=${(ev) => setRow(e.entity_id, { note: ev.target.value })} />
        <div class="ha-flags">
          ${checkbox("default", !!defs[e.entity_id], (v) => setDefault(e.entity_id, v))}
          ${checkbox("in groups", r.inGroup, (v) => setRow(e.entity_id, { inGroup: v }))}
          ${checkbox("exclude", r.exclude, (v) => setRow(e.entity_id, { exclude: v }))}
        </div>
      </div>`;
  };

  return html`
    <div class="stats">
      <div class="tile"><div class="micro">Entities</div><div class="k">${devices.length}</div></div>
      <div class="tile"><div class="micro">Voice-controllable</div><div class="k">${included}</div></div>
      <div class="tile"><div class="micro">With aliases</div><div class="k">${aliasCount}</div></div>
    </div>

    <div class="ha-actions">
      <button class="btn-primary" disabled=${ro || busy || !dirty} onClick=${save}>
        ${busy ? "Saving…" : dirty ? "Save changes" : "Saved"}</button>
      ${dirty ? html`<span class="micro">unsaved changes</span>` : null}
    </div>

    ${ro
      ? html`<p class="micro">Read-only — set <span class="mono">dashboard.controls: true</span>
          to edit curation.</p>`
      : null}
    ${!data.ha_reachable
      ? html`<div class="empty">Couldn't reach Home Assistant to list devices — check
          <span class="mono">HA_API_KEY</span> and <span class="mono">skills.home_assistant.url</span>.
          The bulk exclusions below are still editable.</div>`
      : null}

    <section class="section">
      <header><h2>Bulk exclusions</h2><span class="rule"></span></header>
      <p class="micro">Remove entities from voice control entirely. One pattern per line
        (fnmatch on the entity_id, e.g. <span class="mono">light.*_plug_led</span>).</p>
      <div class="card pad ha-bulk">
        <label class="micro">Patterns
          <textarea rows="3" disabled=${ro} value=${ex.patterns}
            onInput=${(e) => setExField("patterns", e.target.value)}></textarea></label>
        <label class="micro">Domains (comma-separated)
          <input disabled=${ro} value=${ex.domains}
            onInput=${(e) => setExField("domains", e.target.value)} /></label>
        <label class="micro">Areas (comma-separated)
          <input disabled=${ro} value=${ex.areas}
            onInput=${(e) => setExField("areas", e.target.value)} /></label>
      </div>
    </section>

    <section class="section">
      <header><h2>Devices</h2><span class="rule"></span></header>
      <p class="micro"><b>default</b>: part of "turn on the lights" for its room ·
        <b>in groups</b>: included in bare group commands · <b>exclude</b>: hidden from voice.</p>
      ${Object.keys(tree).sort().map(
        (floor) => html`
          <div class="ha-floor" key=${floor}>
            <h3 class="ha-floor-h">${floor}</h3>
            ${Object.keys(tree[floor]).sort().map(
              (area) => html`
                <div class="card ha-area" key=${area}>
                  <div class="ha-area-h">${area}</div>
                  ${Object.keys(tree[floor][area]).sort().map(
                    (domain) => html`
                      <div class="ha-dom" key=${domain}>
                        <div class="ha-dom-h micro">${domain}</div>
                        ${tree[floor][area][domain]
                          .slice()
                          .sort((a, b) => a.entity_id.localeCompare(b.entity_id))
                          .map(row)}
                      </div>`,
                  )}
                </div>`,
            )}
          </div>`,
      )}
      ${devices.length === 0 ? html`<div class="empty">No voice-controllable entities reported.</div>` : null}
    </section>`;
}
