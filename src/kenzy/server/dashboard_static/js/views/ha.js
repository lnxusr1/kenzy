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

// Sub-tabs. All three edit ONE curation file behind ONE Save — this splits the
// screen, not the payload, so the page stops being four stacked editors.
const TABS = [
  { id: "devices", label: "Devices" },
  { id: "presence", label: "Presence sensors" },
  { id: "safety", label: "Safety sensors" },
  { id: "lists", label: "Lists" },
];

export function HaView() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [dev, setDev] = useState({}); // entity_id -> {aliases, note, inGroup, exclude}
  const [defs, setDefs] = useState({}); // entity_id -> bool (in its room's default set)
  const [ex, setEx] = useState({ patterns: "", domains: "", areas: "" });
  // Shopping/to-do lists: which todo entity is "the list" + spoken aliases per list.
  const [listDefault, setListDefault] = useState("");
  const [listAliases, setListAliases] = useState({}); // entity_id -> "csv"
  // v5 presence: entity_id -> bool ("counts as evidence"). Seeded from what the
  // llm actually resolved, so the toggles show live truth, not just the file.
  const [occ, setOcc] = useState({});
  const [showAllOcc, setShowAllOcc] = useState(false);
  // 5.0.6 hazards: entity_id -> bool ("Kenzy announces this"). Same shape as
  // presence above, and stored the same way — only divergence from `auto`.
  const [saf, setSaf] = useState({});
  const [showAllSaf, setShowAllSaf] = useState(false);
  // One curation file, one Save — but four editors' worth of screen. Sub-tabs
  // split the SCREEN, not the payload: build() always reads every section, so
  // saving from any tab writes them all. `edited` remembers which tabs were
  // touched purely so their dot tells you where the unsaved change is.
  const [tab, setTab] = useState("devices");
  const [edited, setEdited] = useState({});
  const [open, setOpen] = useState({}); // "floor/area" -> bool (collapsed by default)
  const [showEx, setShowEx] = useState(false); // bulk-exclusion card (collapsed)

  const dirty = Object.keys(edited).length > 0;
  const setDirty = (where) => setEdited((m) => (where === false ? {} : { ...m, [where]: true }));

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
    const occset = {};
    for (const c of d.occupancy || []) occset[c.entity_id] = !!c.used;
    const safset = {};
    for (const c of d.safety || []) safset[c.entity_id] = !!c.used;
    setSaf(safset);
    setOcc(occset);
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
    const lc = cur.lists || {};
    setListDefault(lc.default || "");
    const la = {};
    for (const [id, arr] of Object.entries(lc.aliases || {})) la[id] = (arr || []).join(", ");
    setListAliases(la);
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
    setDirty("devices");
  };
  const setDefault = (id, on) => {
    setDefs((m) => ({ ...m, [id]: on }));
    setDirty("devices");
  };
  const setExField = (k, v) => {
    setEx((m) => ({ ...m, [k]: v }));
    setDirty("devices");
  };

  // True when this load carries NO trustworthy presence candidates, so the
  // occupancy block must be carried over from the file rather than recomputed.
  // Both halves matter: a non-empty list is trustworthy whatever the flag says,
  // and `!== true` (not `=== false`) means an older kenzy-llm that doesn't send
  // the flag at all is treated as "can't tell" — pairing a newer server with an
  // older llm must not silently discard the operator's edits.
  const occUnavailable = () =>
    (data.occupancy || []).length === 0 && data.occupancy_reachable !== true;
  // Same reasoning as occUnavailable, and it matters for the same reason: the
  // POST replaces curation.yaml wholesale, so a failed hazard query must carry
  // the existing safety rules through rather than compute an empty block.
  const safUnavailable = () =>
    (data.safety || []).length === 0 && data.safety_reachable !== true;

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

    const lists = {};
    if (listDefault) lists.default = listDefault;
    const lal = {};
    for (const [id, csv] of Object.entries(listAliases)) {
      const arr = splitCsv(csv);
      if (arr.length) lal[id] = arr;
    }
    if (Object.keys(lal).length) lists.aliases = lal;
    if (Object.keys(lists).length) out.lists = lists;

    // Presence: record only DIVERGENCE from the automatic behavior, so the file
    // stays small and a later change to the defaults still reaches anyone who
    // never touched a given sensor.
    //
    // This is the one section rebuilt from LIVE data rather than from the file
    // (the others hydrate out of `cur.*`), and the POST replaces curation.yaml
    // wholesale — so when the candidate query failed we must carry the existing
    // rules through untouched instead of computing an empty block from an empty
    // list. Saving an alias on the Devices tab would otherwise delete the
    // operator's presence rules silently. An empty list with a HEALTHY query is
    // a different thing (a house with no presence sensors) and is allowed to
    // clear them.
    if (occUnavailable()) {
      const prev = (data.curation || {}).occupancy;
      if (prev && Object.keys(prev).length) out.occupancy = prev;
    } else {
      const occExclude = [];
      const occInclude = [];
      for (const c of data.occupancy || []) {
        const on = occ[c.entity_id];
        if (on === undefined || on === c.auto) continue;
        (on ? occInclude : occExclude).push(c.entity_id);
      }
      const occOut = {};
      if (occExclude.length) occOut.exclude = occExclude.sort();
      if (occInclude.length) occOut.include = occInclude.sort();
      if (Object.keys(occOut).length) out.occupancy = occOut;
    }

    if (safUnavailable()) {
      const prev = (data.curation || {}).safety;
      if (prev && Object.keys(prev).length) out.safety = prev;
    } else {
      const safExclude = [];
      const safInclude = [];
      for (const c of data.safety || []) {
        const on = saf[c.entity_id];
        if (on === undefined || on === c.auto) continue;
        (on ? safInclude : safExclude).push(c.entity_id);
      }
      const safOut = {};
      if (safExclude.length) safOut.exclude = safExclude.sort();
      if (safInclude.length) safOut.include = safInclude.sort();
      if (Object.keys(safOut).length) out.safety = safOut;
    }

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
  if (data.configured === false)
    return html`<div class="empty">Home Assistant isn't connected yet. Add
      <span class="mono">HA_API_KEY</span> under Settings → API keys, set the
      <span class="mono">home_assistant.url</span> in the llm service config
      (Fleet → llm), restart kenzy-llm — and this screen fills with your
      devices, rooms, and lists.</div>`;

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
  // Collapsed-card summary, so a rule hidden behind the caret still explains why
  // rows below it read "excluded (pattern)".
  const exSummary = [
    [splitLines(ex.patterns).length, "pattern"],
    [splitCsv(ex.domains).length, "domain"],
    [splitCsv(ex.areas).length, "area"],
  ]
    .filter(([n]) => n)
    .map(([n, w]) => `${n} ${w}${n === 1 ? "" : "s"}`)
    .join(" · ");

  const checkbox = (label, checked, onChange) => html`
    <label class="ha-chk"><input type="checkbox" disabled=${ro} checked=${checked}
      onChange=${(e) => onChange(e.target.checked)} /> ${label}</label>`;

  const row = (e) => {
    // Built-in exclusion (Kenzy's own MQTT-bridge entities): code-level, so no
    // curation control could ever apply — render inert instead of implying it.
    if (e.reason === "kenzy internal")
      return html`
        <div class="ha-row excluded" key=${e.entity_id}>
          <div class="ha-ent">
            <span class="mono">${e.entity_id}</span>
            <span class="micro">${e.name} · Kenzy's own entity — always excluded from voice</span>
          </div>
        </div>`;
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
    ${data.skill_disabled
      ? html`<div class="banner warn">
          ⚠ The <b>home assistant</b> module is <b>disabled</b> (Skills tab), so voice
          control and lists are off — nothing on this screen takes effect until it's
          re-enabled. Edits still save, so you can stage changes here first.
        </div>`
      : null}

    <div class="tabbar">
      <div class="tabs" role="tablist">
        ${TABS.map(
          (t) => html`<button key=${t.id} role="tab" aria-selected=${tab === t.id}
            class="tab" onClick=${() => setTab(t.id)}>
            ${t.label}${edited[t.id] ? html`<span class="tab-dot" title="unsaved changes">•</span>` : null}
          </button>`,
        )}
      </div>
      <div class="tabbar-act">
        <button class="btn-primary" disabled=${ro || busy || !dirty} onClick=${save}>
          ${busy ? "Saving…" : dirty ? "Save changes" : "Saved"}</button>
        ${dirty ? html`<span class="micro">unsaved changes</span>` : null}
      </div>
    </div>

    ${ro
      ? html`<p class="micro">Read-only — set <span class="mono">dashboard.controls: true</span>
          to edit curation.</p>`
      : null}
    ${!data.ha_reachable
      ? html`<div class="empty">Couldn't reach Home Assistant to list devices — check
          <span class="mono">HA_API_KEY</span> and <span class="mono">skills.home_assistant.url</span>.
          Bulk exclusions (under Devices) are still editable.</div>`
      : null}

    ${tab !== "safety" ? null : html`<section class="section">
      <p class="micro">Which sensors make Kenzy <strong>speak up on her own</strong>. Smoke,
        carbon monoxide, gas and water-leak sensors count automatically, as does an alarm
        panel once it has actually triggered — armed is not an emergency. She only ever
        repeats what a device asserted; she never decides for herself that something is
        wrong. Turn one OFF when it cries wolf (the smoke sensor above a soldering
        bench), or ON for something unusual that deserves shouting about. Kenzy's own
        entities are never used.</p>
      <p class="micro">Toggle helpers (<span class="mono">input_boolean</span>) are listed
        too and never count unless you tick one — handy for rehearsing the whole path
        without a real detector, or for letting your own Home Assistant automation decide
        something is wrong and flip a switch to say so.</p>
      <p class="micro">Nothing is announced until <span class="mono">proactive.safety.enabled</span>${" "}is on in Settings. When it is, alerts play in <strong>every</strong> room including
        muted ones — say anything to Kenzy to silence one, and it stays silent until that
        sensor goes off and trips again.</p>
      ${(data.safety || []).length === 0
        ? safUnavailable()
          ? html`<div class="empty">Couldn't reach Home Assistant to list hazard sensors.
              Your existing choices are preserved — saving from another tab won't
              discard them — but they can't be edited until this loads.</div>`
          : html`<div class="empty">No hazard-capable sensors found in Home Assistant.</div>`
        : (() => {
            const all = data.safety || [];
            const isPrimary = (c) => c.auto || c.used || (saf[c.entity_id] !== undefined && saf[c.entity_id] !== c.auto);
            const primary = all.filter(isPrimary);
            const others = all.filter((c) => !isPrimary(c));
            const row = (c) => html`<label class="occ-row" key=${c.entity_id}>
              <input type="checkbox" disabled=${ro}
                checked=${saf[c.entity_id] === undefined ? !!c.used : !!saf[c.entity_id]}
                onChange=${(e) => {
                  setSaf((m) => ({ ...m, [c.entity_id]: e.target.checked }));
                  setDirty("safety");
                }} />
              <span class="occ-name">${c.name}</span>
              <span class="micro occ-where">${c.area_name || "no room"}</span>
              <span class="micro occ-class">${c.hazard || c.device_class || "—"}</span>
              <span class="mono occ-id">${c.entity_id}</span>
            </label>`;
            return html`<div class="card pad">
              ${primary.length ? primary.map(row) : html`<div class="empty">Nothing counts as a hazard yet.</div>`}
              ${others.length
                ? html`<div class="occ-more">
                    <button class="btn-ghost" onClick=${() => setShowAllSaf(!showAllSaf)}>
                      ${showAllSaf ? "Hide" : "Show"} ${others.length} other sensor${others.length === 1 ? "" : "s"}
                    </button>
                  </div>
                  ${showAllSaf ? others.map(row) : null}`
                : null}
            </div>`;
          })()}
    </section>`}

    ${tab !== "lists" ? null : html`<section class="section">
      <p class="micro">Shopping/to-do voice commands ("add milk to the list") use HA's
        to-do lists. Pick which one a bare "the list" means, and add spoken aliases
        ("the groceries"). No lists here? Add HA's <b>Local to-do</b> integration.</p>
      ${(data.lists || []).length === 0
        ? html`<div class="empty">No to-do lists found in Home Assistant.</div>`
        : html`<div class="card pad ha-bulk">
            <label class="micro">Default list ("the list")
              <select disabled=${ro} value=${listDefault}
                onChange=${(e) => { setListDefault(e.target.value); setDirty("lists"); }}>
                <option value="">${(data.lists || []).length === 1 ? "(the only list)" : "(ask which list)"}</option>
                ${(data.lists || []).map(
                  (l) => html`<option value=${l.entity_id}>${l.name}</option>`,
                )}
              </select></label>
            ${/* Label text in ONE span: .ha-bulk label is a flex column, so each
                  bare chunk would otherwise land on its own line. */ ""}
            ${(data.lists || []).map(
              (l) => html`
                <label class="micro" key=${l.entity_id}>
                  <span>${l.name} <span class="mono">${l.entity_id}</span> — aliases (comma-separated)</span>
                  <input disabled=${ro} placeholder="the groceries, grocery list"
                    value=${listAliases[l.entity_id] || ""}
                    onInput=${(e) => {
                      setListAliases((m) => ({ ...m, [l.entity_id]: e.target.value }));
                      setDirty("lists");
                    }} /></label>`,
            )}
          </div>`}
    </section>`}

    ${tab !== "presence" ? null : html`<section class="section">
      <p class="micro">Which sensors tell Kenzy a room has someone in it (shown on the
        Presence tab). Motion, occupancy and presence sensors count automatically, as do
        HA's person entities. Turn one OFF when it lies — a hallway sensor the cat trips,
        or one aimed through a window at the street — or ON to use a sensor that isn't a
        presence type. Kenzy's own entities are never used.</p>
      ${(data.occupancy || []).length === 0
        ? occUnavailable()
          ? html`<div class="empty">Couldn't reach Home Assistant to list presence sensors.
              Your existing choices are preserved — saving from another tab won't
              discard them — but they can't be edited until this loads.</div>`
          : html`<div class="empty">No presence-capable sensors found in Home Assistant.</div>`
        : (() => {
            // A real house has one connectivity sensor per cloud light — over a
            // hundred rows of noise. Show what actually counts (or has been
            // deliberately changed); keep the rest one click away so a door
            // sensor can still be opted in.
            const all = data.occupancy || [];
            const isPrimary = (c) => c.auto || c.used || occ[c.entity_id] !== undefined && occ[c.entity_id] !== c.auto;
            const primary = all.filter(isPrimary);
            const others = all.filter((c) => !isPrimary(c));
            const row = (c) => html`<label class="occ-row" key=${c.entity_id}>
              <input type="checkbox" disabled=${ro}
                checked=${occ[c.entity_id] === undefined ? !!c.used : !!occ[c.entity_id]}
                onChange=${(e) => {
                  setOcc((m) => ({ ...m, [c.entity_id]: e.target.checked }));
                  setDirty("presence");
                }} />
              <span class="occ-name">${c.name}</span>
              <span class="micro occ-where">${c.scope === "house" ? "whole house" : c.area_name || "no room"}</span>
              <span class="micro occ-class">${c.device_class || "—"}${c.kind ? " · " + c.kind : ""}</span>
              <span class="mono occ-id">${c.entity_id}</span>
            </label>`;
            return html`<div class="card pad">
              ${primary.length ? primary.map(row) : html`<div class="empty">Nothing counts as presence yet.</div>`}
              ${others.length
                ? html`<div class="occ-more">
                    <button class="btn-ghost" onClick=${() => setShowAllOcc(!showAllOcc)}>
                      ${showAllOcc ? "Hide" : "Show"} ${others.length} other sensor${others.length === 1 ? "" : "s"}
                    </button>
                  </div>
                  ${showAllOcc ? others.map(row) : null}`
                : null}
            </div>`;
          })()}
    </section>`}

    ${tab !== "devices" ? null : html`<section class="section">
      <div class="stats">
        <div class="tile"><div class="micro">Entities</div><div class="k">${devices.length}</div></div>
        <div class="tile"><div class="micro">Voice-controllable</div><div class="k">${included}</div></div>
        <div class="tile"><div class="micro">With aliases</div><div class="k">${aliasCount}</div></div>
      </div>
      <p class="micro"><b>default</b>: part of "turn on the lights" for its room ·
        <b>in groups</b>: included in bare group commands · <b>exclude</b>: hidden from voice.</p>

      <div class="card pad ha-ex">
        <button class="ha-ex-h" aria-expanded=${showEx} onClick=${() => setShowEx(!showEx)}>
          <span class="ha-caret">${showEx ? "▾" : "▸"}</span> Bulk exclusions
          <span class="micro">${exSummary || "none"}</span>
        </button>
        ${showEx
          ? html`<div class="ha-bulk">
              <p class="micro">Remove entities from voice control by rule, instead of one row at
                a time. One pattern per line (fnmatch on the entity_id, e.g.
                <span class="mono">light.*_plug_led</span>).</p>
              <label class="micro">Patterns
                <textarea rows="3" disabled=${ro} value=${ex.patterns}
                  onInput=${(e) => setExField("patterns", e.target.value)}></textarea></label>
              <label class="micro">Domains (comma-separated)
                <input disabled=${ro} value=${ex.domains}
                  onInput=${(e) => setExField("domains", e.target.value)} /></label>
              <label class="micro">Areas (comma-separated)
                <input disabled=${ro} value=${ex.areas}
                  onInput=${(e) => setExField("areas", e.target.value)} /></label>
            </div>`
          : null}
      </div>
      ${Object.keys(tree).sort().map(
        (floor) => html`
          <div class="ha-floor" key=${floor}>
            <h3 class="ha-floor-h">${floor}</h3>
            ${Object.keys(tree[floor]).sort().map((area) => {
              const key = `${floor}/${area}`;
              const isOpen = !!open[key];
              const ents = Object.values(tree[floor][area]).flat();
              const nIncl = ents.filter((e) => e.included).length;
              return html`
                <div class="card ha-area" key=${area}>
                  <button
                    class="ha-area-h ha-area-toggle"
                    aria-expanded=${isOpen}
                    onClick=${() => setOpen((m) => ({ ...m, [key]: !isOpen }))}
                  >
                    <span class="ha-caret">${isOpen ? "▾" : "▸"}</span> ${area}
                    <span class="micro ha-area-count">
                      ${ents.length} device${ents.length === 1 ? "" : "s"}${nIncl < ents.length ? `, ${nIncl} voice-controllable` : ""}
                    </span>
                  </button>
                  ${isOpen
                    ? Object.keys(tree[floor][area]).sort().map(
                        (domain) => html`
                          <div class="ha-dom" key=${domain}>
                            <div class="ha-dom-h micro">${domain}</div>
                            ${tree[floor][area][domain]
                              .slice()
                              .sort((a, b) => a.entity_id.localeCompare(b.entity_id))
                              .map(row)}
                          </div>`,
                      )
                    : null}
                </div>`;
            })}
          </div>`,
      )}
      ${devices.length === 0 ? html`<div class="empty">No voice-controllable entities reported.</div>` : null}
    </section>`}`;
}
