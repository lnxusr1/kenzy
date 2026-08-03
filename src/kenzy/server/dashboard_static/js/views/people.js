import { html, useState, useEffect } from "../html.js";
import { confirmDialog } from "../dialog.js";
import { getMemory, getPeople } from "../api.js";
import { send, notify, subscribeMemory } from "../store.js";

// People: the one surface for who Kenzy knows and what she knows about them.
// The LIST page shows each household member (voice status + memory count) with
// a drill-down, plus the page-level buckets that belong to no single person:
// household-shared memory, voices without a person, unowned facts, and a
// search across every remembered fact. The DETAIL page (person drill-down,
// like node → config) carries identity editing, voice enrollment, deletion,
// and the person's own memories. Person-first invariants unchanged: a person
// may be voiceless, but every enrolled voice belongs to a person; memory works
// only for voices linked to a person. Tiers gate *voices* — this credentialed
// admin surface sees every tier.

const TIER_LABEL = {
  private: "private",
  "personal-public": "about them",
  shared: "shared",
};
const TIER_BADGE = {
  private: "badge tier",
  "personal-public": "badge",
  shared: "badge fast",
};


// Normalized memory search — mirrors the ledger's tokenizer in
// kenzy/llm/memory.py (_tokens; keep the stopword list in sync). Punctuated
// words match both joined and split forms ("wifi" finds "Wi-Fi"), stopwords
// ("is", "on", "the", …) never match anything, and every remaining query token
// must prefix-match some fact token — so typing narrows instead of flooding.
const STOPWORDS = new Set(
  ("a an and are be did do does for from had has have how i in is it me my of on or " +
   "s t that the their them they this to was we what when where which who will you your").split(" "),
);

function factTokens(text) {
  const out = new Set();
  for (const word of text.toLowerCase().match(/[a-z0-9]+(?:['\-._][a-z0-9]+)*/g) || []) {
    const joined = word.replace(/[^a-z0-9]/g, "");
    if (joined && !STOPWORDS.has(joined)) out.add(joined);
    if (joined !== word)
      for (const part of word.split(/[^a-z0-9]+/))
        if (part && !STOPWORDS.has(part)) out.add(part);
  }
  return out;
}

function factMatches(query, text) {
  const q = [...factTokens(query)];
  if (!q.length) return false; // stopword-only query carries no signal
  const t = [...factTokens(text)];
  return q.every((qt) => t.some((tt) => tt.startsWith(qt)));
}

function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

// The drill-down selection is OWNED BY THE SHELL (passed as selected/onSelect,
// like ServicesView) so clicking "People" in the sidebar always returns to the
// list — held locally, the nav click would be a no-op re-render of the detail.
export function PeopleView({ selected, onSelect, onConfigureService }) {
  const [people, setPeople] = useState(null); // /api/people payload
  const [mem, setMem] = useState(null); // /api/memory payload
  const [err, setErr] = useState("");

  const load = () =>
    Promise.all([getPeople(), getMemory()])
      .then(([p, m]) => {
        setPeople(p);
        setMem(m);
      })
      .catch((e) => setErr(String(e)));

  useEffect(() => {
    load();
    // Live: the server pokes on any memory/lockbox change (4.2) — the poll
    // drops to a slow WS-down fallback, like the Scheduled tab.
    const unsub = subscribeMemory(load);
    const t = setInterval(load, 60000);
    return () => { clearInterval(t); unsub(); };
  }, []);

  if (err) return html`<div class="empty">Could not load people: ${err}</div>`;
  if (!people || !mem) return html`<div class="empty">Loading…</div>`;

  const facts = mem.reachable ? mem.facts || [] : [];
  const person = selected && (people.people || []).find((p) => p.id === selected);
  if (person)
    return html`<${PersonDetail} person=${person} data=${people} facts=${facts}
      secrets=${(mem.lockbox && mem.lockbox.secrets) || []}
      memReachable=${mem.reachable} onBack=${() => onSelect(null)} reload=${load} />`;
  return html`<${PeopleList} data=${people} mem=${mem} facts=${facts}
    onOpen=${onSelect} reload=${load} onConfigureService=${onConfigureService} />`;
}

// ---------------------------------------------------------------------------
// List page
// ---------------------------------------------------------------------------

function PeopleList({ data, mem, facts, onOpen, reload, onConfigureService }) {
  const [adding, setAdding] = useState(false);
  const [newName, setNewName] = useState("");
  const [newVoices, setNewVoices] = useState([]);
  const [busy, setBusy] = useState("");
  const [q, setQ] = useState("");

  const controls = data.controls;
  const persons = data.people || [];
  const voiceprints = data.voiceprints || [];
  const unassigned = voiceprints.filter((v) => !v.person_id);
  const personIds = new Set(persons.map((p) => p.id));
  const shared = facts.filter((f) => f.tier === "shared");
  const unowned = facts.filter((f) => !personIds.has(f.owner) && f.tier !== "shared");
  const voicesOf = (p) => voiceprints.filter((v) => v.person_id === p.id);
  // Shared facts are household property (listed under Household memory only),
  // so they don't count as a person's own memories.
  const memoryCount = (p) =>
    facts.filter((f) => f.owner === p.id && f.tier !== "shared").length;

  const summary = (p) => {
    const voices = voicesOf(p);
    const samples = voices.reduce((n, v) => n + (v.samples || 0), 0);
    const voicePart = voices.length
      ? `voice · ${samples} sample${samples === 1 ? "" : "s"}`
      : "no voice yet";
    const n = memoryCount(p);
    const memPart = mem.reachable
      ? n
        ? `${n} memor${n === 1 ? "y" : "ies"}`
        : "no memories"
      : null;
    const parts = [voicePart, memPart, p.ha_user ? "HA app" : null].filter(Boolean);
    return parts.join(" · ");
  };

  async function saveNew() {
    const name = newName.trim();
    if (!name) return;
    setBusy("new");
    // person id rides as `person_id`: store.send() spreads the payload over the
    // envelope, and a payload `id` would clobber the request/ack correlation id.
    const res = await send("save_person", { person_id: "", name, voiceprints: newVoices });
    setBusy("");
    if (res.ok) {
      setAdding(false);
      setNewName("");
      setNewVoices([]);
      await reload();
      notify(`Saved ${name}.`);
    } else notify(res.error || "Could not save.", "err");
  }

  async function delVoice(v) {
    if (!(await confirmDialog(`Delete the voice profile “${v.name}”? This can't be undone.`, { title: "Delete voice", confirmText: "Delete", danger: true }))) return;
    setBusy("vp:" + v.name);
    const res = await send("delete_speaker", { name: v.name });
    setBusy("");
    if (res.ok) {
      await reload();
      notify(`Deleted ${v.name}.`);
    } else notify(res.error || "Delete failed.", "err");
  }

  const searching = !!q.trim();
  const hits = searching ? facts.filter((f) => factMatches(q, f.text)) : [];

  return html`
    <div class="stats">
      <div class="tile"><div class="micro">People</div><div class="k">${persons.length}</div></div>
      <div class="tile"><div class="micro">Enrolled voices</div><div class="k">${voiceprints.length}</div></div>
      <div class="tile"><div class="micro">Remembered facts</div>
        <div class="k">${mem.reachable ? facts.length : "—"}</div></div>
    </div>

    ${mem.reachable && mem.local_model === false
      ? html`<div class="banner warn">⚠ No local model is set for memory upkeep — ambiguous
          memories stay <b>held for review</b> below, and restated private memories aren't
          merged, so spoken answers can pick up duplicates. Set
          <span class="mono">memory.classifier_model</span> +
          <span class="mono">classifier_url</span> to a local model (e.g. Ollama) to fix both.
          ${onConfigureService
            ? html`<button class="btn-ghost" style="margin-left:10px"
                onClick=${() => onConfigureService("llm")}>Open LLM settings</button>`
            : null}
        </div>`
      : null}
    ${!data.speaker_reachable
      ? html`<div class="banner warn">⚠ The speaker service isn't reachable, so enrolled voices
          can't be listed or managed. Check <span class="mono">speaker.url</span> and that
          ${" "}<span class="mono">kenzy-speaker</span> is running.</div>`
      : null}

    <section class="section">
      <header><h2>People</h2><span class="rule"></span>
        <button class="btn-ghost" onClick=${reload} title="Re-fetch memories and lockbox">⟳ Refresh</button>
      </header>
      <p class="micro">A person is a household member Kenzy knows by name. Open one to manage
        their identity, voice, and what she remembers for them.</p>

      ${controls
        ? html`<div class="ppl-actions">
            <button class="btn-primary" disabled=${adding} onClick=${() => setAdding(true)}>
              + Add person</button>
          </div>`
        : html`<p class="micro">Read-only — set <span class="mono">dashboard.controls: true</span>
            to add or edit people.</p>`}

      ${adding
        ? html`<div class="card ppl-card ppl-editing">
            <div class="ppl-head"><span class="ppl-name">New person</span></div>
            <div class="ppl-form">
              <label class="field">
                <span class="micro">Name</span>
                <input autofocus placeholder="e.g. Alice" value=${newName}
                  onInput=${(e) => setNewName(e.target.value)}
                  onKeyDown=${(e) => {
                    if (e.key === "Enter") saveNew();
                    if (e.key === "Escape") setAdding(false);
                  }} />
              </label>
              ${unassigned.length
                ? html`<div class="field">
                    <span class="micro">Link an existing voice (optional)</span>
                    <div class="ppl-voices">
                      ${unassigned.map((v) => {
                        const on = newVoices.includes(v.name);
                        return html`<label class="ha-chk" key=${v.name}>
                          <input type="checkbox" checked=${on}
                            onChange=${() =>
                              setNewVoices(
                                on ? newVoices.filter((x) => x !== v.name) : [...newVoices, v.name],
                              )} />
                          <span class="mono">${v.name}</span>
                        </label>`;
                      })}
                    </div>
                  </div>`
                : null}
              <div class="ppl-form-actions">
                <button class="btn-primary" disabled=${!newName.trim() || busy === "new"}
                  onClick=${saveNew}>${busy === "new" ? "Saving…" : "Save"}</button>
                <button class="btn-ghost" onClick=${() => setAdding(false)}>Cancel</button>
              </div>
            </div>
          </div>`
        : null}

      ${persons.length === 0 && !adding
        ? html`<div class="empty">No people yet. ${controls
            ? "Add one above, then enroll their voice."
            : "Enable controls to add one."}</div>`
        : html`<div class="ppl-list">
            ${persons.map(
              (p) => html`
                <div class="card ppl-card" key=${p.id}>
                  <div class="ppl-head">
                    <button class="ppl-title" onClick=${() => onOpen(p.id)}>
                      <span class="sk-chev" aria-hidden="true">▸</span>
                      <span class="ppl-name">${p.name}</span>
                    </button>
                    <span class=${"ppl-owner" + (voicesOf(p).length ? "" : " ppl-unowned")}>
                      ${summary(p)}</span>
                    <div class="ppl-voice-actions">
                      <button class="btn-ghost" onClick=${() => onOpen(p.id)}>Open</button>
                    </div>
                  </div>
                </div>`,
            )}
          </div>`}
    </section>

    ${mem.reachable
      ? html`<section class="section">
          <header><h2>Search memory</h2><span class="rule"></span></header>
          <div class="mem-filters">
            <input class="mem-search" placeholder="search every remembered fact…" value=${q}
              onInput=${(e) => setQ(e.target.value)} />
          </div>
          ${searching
            ? hits.length
              ? html`<div class="card mem-list">
                  ${hits.map((f) => html`<${FactRow} f=${f} key=${f.id}
                    controls=${controls} reload=${reload} showOwner=${true} />`)}
                </div>`
              : html`<div class="empty">No facts match.</div>`
            : null}
        </section>`
      : html`<div class="banner warn">⚠ Memory isn't reachable — check that
          ${" "}<span class="mono">kenzy-llm</span> is running (and
          ${" "}<span class="mono">memory.enabled</span> isn't false).</div>`}

    ${mem.reachable && shared.length
      ? html`<section class="section">
          <header><h2>Household memory</h2><span class="rule"></span></header>
          <p class="micro">Shared facts — the whole house can ask for these, and anyone
            recognized can add or erase them by voice.</p>
          <div class="card mem-list">
            ${shared.map((f) => html`<${FactRow} f=${f} key=${f.id}
              controls=${controls} reload=${reload} showOwner=${true} />`)}
          </div>
        </section>`
      : null}

    ${unassigned.length && data.speaker_reachable
      ? html`<section class="section">
          <header><h2>Voices without a person</h2><span class="rule"></span></header>
          <p class="micro">Enrolled voices not linked to anyone (from an older setup or the
            ${" "}<span class="mono">kenzy-enroll</span> CLI). Add a person above and link
            them — or delete the profile.</p>
          <div class="card ppl-voice-list">
            ${unassigned.map(
              (v) => html`
                <div class="ppl-voice-row" key=${v.name}>
                  <div class="ppl-voice">
                    <span class="mono">${v.name}</span>
                    <span class="micro">${v.samples} sample${v.samples === 1 ? "" : "s"}</span>
                  </div>
                  <div class="ppl-voice-actions">
                    <button class="btn-ghost danger" disabled=${!controls || busy === "vp:" + v.name}
                      onClick=${() => delVoice(v)}>${busy === "vp:" + v.name ? "…" : "Delete"}</button>
                  </div>
                </div>`,
            )}
          </div>
        </section>`
      : null}

    ${mem.reachable && unowned.length
      ? html`<section class="section">
          <header><h2>Facts without a person</h2><span class="rule"></span></header>
          <p class="micro">Remembered for someone whose person record was deleted. Forget
            them, or recreate the person with the same id to reclaim them.</p>
          <div class="card mem-list">
            ${unowned.map((f) => html`<${FactRow} f=${f} key=${f.id}
              controls=${controls} reload=${reload} showOwner=${true} />`)}
          </div>
        </section>`
      : null}`;
}

// ---------------------------------------------------------------------------
// Person detail (drill-down)
// ---------------------------------------------------------------------------

function PersonDetail({ person, data, facts, secrets, memReachable, onBack, reload }) {
  const [name, setName] = useState(person.name);
  const [aliasText, setAliasText] = useState((person.aliases || []).join(", "));
  const [voices, setVoices] = useState([...person.voiceprints]);
  const [haUser, setHaUser] = useState(person.ha_user || "");
  const [memOptOut, setMemOptOut] = useState(!!person.memory_opt_out);
  const [memCapture, setMemCapture] = useState(person.memory_capture || "explicit");
  const [busy, setBusy] = useState("");
  const [enrolling, setEnrolling] = useState(false);
  const [enrollRoom, setEnrollRoom] = useState("");
  const [q, setQ] = useState("");

  const controls = data.controls;
  const rooms = data.rooms || [];
  const voiceprints = data.voiceprints || [];
  // Offer this person's voices + unowned ones (moving voices between people is
  // no longer a first-class flow — enrollment is per-person).
  const offer = voiceprints.filter((v) => !v.person_id || v.person_id === person.id);
  // Only what Kenzy holds FOR them — shared facts live under Household memory
  // on the People page, so cleaning up here can't delete household facts.
  const owned = facts.filter((f) => f.owner === person.id && f.tier !== "shared");
  const shown = q.trim() ? owned.filter((f) => factMatches(q, f.text)) : owned;
  const aliasList = aliasText.split(",").map((a) => a.trim()).filter(Boolean);
  const dirty =
    name.trim() !== person.name ||
    JSON.stringify(aliasList) !== JSON.stringify(person.aliases || []) ||
    haUser.trim() !== (person.ha_user || "") ||
    memOptOut !== !!person.memory_opt_out ||
    memCapture !== (person.memory_capture || "explicit") ||
    JSON.stringify([...voices].sort()) !== JSON.stringify([...person.voiceprints].sort());

  async function save() {
    if (!name.trim()) return;
    setBusy("save");
    // Mirror the server's normalization (bare object_id → person.<id>) so the
    // field matches the stored value after save instead of reading as dirty.
    let ha = haUser.trim().toLowerCase();
    if (ha && !ha.includes(".")) ha = `person.${ha}`;
    const res = await send("save_person", {
      person_id: person.id,
      name: name.trim(),
      voiceprints: voices,
      aliases: aliasList,
      ha_user: ha,
      memory_opt_out: memOptOut,
      memory_capture: memCapture,
    });
    setBusy("");
    if (res.ok) {
      setHaUser(ha);
      // Turning the opt-out ON offers to also erase what's already stored —
      // "don't remember me" usually means the existing facts too. Shared
      // facts stay with the house (same rule as Remove completely).
      if (memOptOut && !person.memory_opt_out && owned.length) {
        const n = owned.length;
        if (
          await confirmDialog(
            `Also erase the ${n} fact${n === 1 ? "" : "s"} Kenzy already holds for ${name.trim()}? ` +
              `Facts they shared with the house stay. This can't be undone.`,
            { title: "Erase their memories?", confirmText: "Erase", cancelText: "Keep them", danger: true },
          )
        ) {
          const r = await send("erase_person_memory", { person_id: person.id });
          if (r.ok) notify(`Erased ${name.trim()}'s stored facts.`);
          else notify(r.error || "Could not erase their facts.", "err");
        }
      }
      await reload();
      notify(`Saved ${name.trim()}.`);
    } else notify(res.error || "Could not save.", "err");
  }

  async function delPerson() {
    const also = owned.length
      ? ` Their ${owned.length} remembered fact${owned.length === 1 ? "" : "s"} will show under "Facts without a person" until forgotten.`
      : "";
    if (!(await confirmDialog(`Delete “${person.name}”? Their enrolled voice stays and can be relinked.${also}`, { title: "Delete person", confirmText: "Delete", danger: true })))
      return;
    setBusy("del");
    const res = await send("delete_person", { person_id: person.id });
    setBusy("");
    if (res.ok) {
      notify(`Deleted ${person.name}.`);
      onBack();
      await reload();
    } else notify(res.error || "Delete failed.", "err");
  }

  // The "HA person" field only exists for households where HA is in the
  // picture (configured, or the app front door in use). Four states:
  // module disabled → read-only note; no HA at all → hidden (editable only
  // if a mapping already exists); HA reachable → dropdown of real persons;
  // configured but unreachable → free-text fallback.
  function haField() {
    const ha = data.ha || {};
    const label = html`<span class="micro">Home Assistant person — links their
      HA app login so Assist requests arrive as them (blank = not linked)</span>`;
    if (ha.skill_disabled) {
      return haUser.trim()
        ? html`<div class="field"><span class="micro">HA app:
            ${" "}<span class="mono">${haUser}</span> — Home Assistant features are
            disabled in Skills, so this link is inactive.</span></div>`
        : null;
    }
    if (!ha.configured && !ha.assist_seen && !haUser.trim()) return null;
    const persons = ha.persons || [];
    if (persons.length) {
      const known = persons.some((p) => p.entity_id === haUser.trim());
      return html`<label class="field">
        ${label}
        <select class="audio-select" value=${haUser} disabled=${!controls}
          onChange=${(e) => setHaUser(e.target.value)}>
          <option value="">— not linked —</option>
          ${!known && haUser.trim()
            ? html`<option value=${haUser}>${haUser} (not in HA)</option>`
            : null}
          ${persons.map(
            (p) => html`<option value=${p.entity_id} key=${p.entity_id}>
              ${p.name} (${p.entity_id})</option>`,
          )}
        </select>
      </label>`;
    }
    return html`<label class="field">
      ${label}
      <input value=${haUser} disabled=${!controls} placeholder="person.…"
        onInput=${(e) => setHaUser(e.target.value)} />
    </label>`;
  }

  const [expSecrets, setExpSecrets] = useState(true);
  function exportPerson() {
    const a = document.createElement("a");
    a.href = `/api/people/${encodeURIComponent(person.id)}/export?secrets=${expSecrets ? 1 : 0}`;
    a.download = `kenzy-${person.id}-export.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  async function revokePerson() {
    const facts = owned.length;
    const vp = person.voiceprints.length;
    const okd = await confirmDialog(
      `Remove ${person.name} completely: erase their ${facts} remembered fact${facts === 1 ? "" : "s"}, ` +
        `delete ${vp} enrolled voice${vp === 1 ? "" : "s"}, and remove their person record. ` +
        `Household-shared facts they contributed stay with the house. This cannot be undone.`,
      { title: "Remove completely", confirmText: "Remove", danger: true, typed: "REMOVE" },
    );
    if (!okd) return;
    setBusy("revoke");
    const res = await send("revoke_person", { person_id: person.id });
    setBusy("");
    if (res.ok) {
      notify(`${person.name} has been removed — Kenzy no longer knows them.`);
      onBack();
      await reload();
    } else notify(res.error || "Revoke failed.", "err");
  }

  async function startEnroll() {
    const res = await send("enroll_speaker", { person_id: person.id, node: enrollRoom });
    if (res.ok) {
      setEnrolling(false);
      notify(`Enrolling ${person.name} — speak when prompted. Sample counts update when it finishes.`);
    } else notify(res.error || "Could not start enrollment.", "err");
  }

  return html`
    <div class="cfg">
      <button class="btn-ghost back" onClick=${onBack}>← People</button>

      <section class="section">
        <header><h2>${person.name}</h2><span class="rule"></span>
          <button class="btn-ghost" onClick=${reload} title="Re-fetch memories and lockbox">⟳ Refresh</button>
        </header>
        <div class="card pad">
          <label class="field">
            <span class="micro">Name</span>
            <input value=${name} disabled=${!controls}
              onInput=${(e) => setName(e.target.value)} />
          </label>
          <label class="field">
            <span class="micro">Also called — nicknames and spellings the speech
              recognizer produces for them ("Bobby" for Bobbie); comma-separated.
              Close spellings already match on their own — this is for the rest.</span>
            <input value=${aliasText} disabled=${!controls}
              onInput=${(e) => setAliasText(e.target.value)} />
          </label>
          ${haField()}
          <div class="field">
            <span class="micro">Voices — which enrolled voices are this person</span>
            ${offer.length === 0
              ? html`<p class="micro">No voice yet — enroll one below.</p>`
              : html`<div class="ppl-voices">
                  ${offer.map((v) => {
                    const on = voices.some((x) => x.toLowerCase() === v.name.toLowerCase());
                    return html`<label class="ha-chk" key=${v.name}>
                      <input type="checkbox" disabled=${!controls} checked=${on}
                        onChange=${() =>
                          setVoices(
                            on
                              ? voices.filter((x) => x.toLowerCase() !== v.name.toLowerCase())
                              : [...voices, v.name],
                          )} />
                      <span class="mono">${v.name}</span>${" "}
                      <span class="micro">${v.samples} samples</span>
                    </label>`;
                  })}
                </div>`}
          </div>
          ${controls
            ? html`<div class="ppl-form-actions">
                <button class="btn-primary" disabled=${!dirty || !name.trim() || busy === "save"}
                  onClick=${save}>${busy === "save" ? "Saving…" : dirty ? "Save" : "Saved"}</button>
                <button class="btn-ghost" disabled=${!rooms.length || enrolling}
                  title=${rooms.length ? "" : "no room nodes connected"}
                  onClick=${() => {
                    setEnrolling(true);
                    setEnrollRoom(rooms[0] ? rooms[0].node_id : "");
                  }}>Enroll voice</button>
                <button class="btn-ghost danger" disabled=${busy === "del"}
                  onClick=${delPerson}>${busy === "del" ? "…" : "Delete person"}</button>
              </div>`
            : null}
          ${enrolling
            ? html`<div class="ppl-enroll-row">
                <span class="micro">Enroll from</span>
                <select class="audio-select" value=${enrollRoom} onChange=${(e) => setEnrollRoom(e.target.value)}>
                  ${rooms.map(
                    (r) => html`<option value=${r.node_id}>${r.room || r.node_id}</option>`,
                  )}
                </select>
                <button class="btn-primary" disabled=${!enrollRoom} onClick=${startEnroll}>
                  Start</button>
                <button class="btn-ghost" onClick=${() => setEnrolling(false)}>Cancel</button>
              </div>`
            : null}
        </div>
      </section>

      <section class="section">
        <header><h2>Memories</h2><span class="rule"></span></header>
        <p class="micro">What Kenzy holds for ${person.name} — <b>private</b> facts are only
          ever spoken back to them. Facts they've shared with the house live under
          Household memory on the People page, not here. Say
          ${" "}<span class="mono">"Hey Kenzy, remember that…"</span> to add more.</p>
        ${!memReachable
          ? html`<div class="empty">Memory isn't reachable right now.</div>`
          : owned.length === 0
            ? html`<div class="empty">Nothing remembered for ${person.name} yet.</div>`
            : html`
                ${owned.length > 5
                  ? html`<div class="mem-filters">
                      <input class="mem-search" placeholder="search their facts…" value=${q}
                        onInput=${(e) => setQ(e.target.value)} />
                    </div>`
                  : null}
                ${shown.length === 0
                  ? html`<div class="empty">No facts match.</div>`
                  : html`<div class="card mem-list">
                      ${shown.map((f) => html`<${FactRow} f=${f} key=${f.id}
                        controls=${controls} reload=${reload} showOwner=${false} />`)}
                    </div>`}
              `}
      </section>

      ${(secrets || []).filter((x) => x.owner === person.id).length
        ? html`<section class="section">
            <header><h2>Lockbox</h2><span class="rule"></span></header>
            <p class="micro">Secrets ${person.name} asked Kenzy to lock away —
              encrypted on disk, never sent to any model, and only ever spoken
              back to them. Shown here only when you click <b>Reveal</b>. Say
              ${" "}<span class="mono">"remember this secretly…"</span> to add one.</p>
            <div class="card mem-list">
              ${secrets.filter((x) => x.owner === person.id).map(
                (x) => html`<${SecretRow} s=${x} key=${x.id} controls=${data.controls} reload=${reload} />`,
              )}
            </div>
          </section>`
        : null}

      <section class="section">
        <header><h2>Privacy & data</h2><span class="rule"></span></header>
        <div class="card pad">
          <label class="field">
            <span class="micro">Memory capture — when Kenzy remembers things
              for${" "}${person.name}. <b>Explicit</b>: only when asked ("remember that…").
              <b>Suggest</b>: she asks aloud before remembering — nothing
              stored without your spoken yes. <b>Auto</b>: she remembers on her own and always says
              so — her picks show a "auto" tag below, each a Forget away.</span>
            <select class="audio-select" value=${memCapture} disabled=${!controls || memOptOut}
              onChange=${(e) => setMemCapture(e.target.value)}>
              <option value="explicit">Explicit — only when asked (default)</option>
              <option value="suggest">Suggest — ask before remembering</option>
              <option value="auto">Auto — remember proactively, say so</option>
            </select>
          </label>
          <label class="ha-chk">
            <input type="checkbox" disabled=${!controls} checked=${memOptOut}
              onChange=${(e) => setMemOptOut(e.target.checked)} />
            <span>Don't remember ${person.name} — Kenzy keeps and reads no facts
              about them (they stay a recognized voice for device control and
              questions).${memOptOut !== !!person.memory_opt_out ? " Save to apply." : ""}</span>
          </label>
          <label class="ha-chk micro">
            <input type="checkbox" checked=${expSecrets}
              onChange=${(e) => setExpSecrets(e.target.checked)} />
            ${" "}Include lockbox secrets in the export (same access as Reveal; untick for a
            shareable file)
          </label>
          <div class="ppl-form-actions">
            <button class="btn-ghost" onClick=${exportPerson}
              title="Download their person record, voice-profile info, every remembered fact, and (by default) their lockbox entries as a file">
              Export their data</button>
            ${controls
              ? html`<button class="btn-ghost danger" disabled=${busy === "revoke"}
                  onClick=${revokePerson}
                  title="Erase their facts, delete their voice, and remove them — the guest-departure case">
                  ${busy === "revoke" ? "Removing…" : "Remove completely"}</button>`
              : null}
          </div>
          <p class="micro">Export answers "what does Kenzy know about me". Remove
            is total: memories, voice, record — household-shared facts they
            contributed stay with the house.</p>
        </div>
      </section>
    </div>`;
}

// ---------------------------------------------------------------------------
// One remembered fact (shared by the detail page and the page-level buckets)
// ---------------------------------------------------------------------------

// One lockbox secret: masked by default; Reveal fetches the text over the
// controls-gated endpoint, shows it inline, and auto-hides after 30s. The
// list payload never contains secret text — only this explicit click does.
function SecretRow({ s, controls, reload }) {
  const [busy, setBusy] = useState(false);
  const [shown, setShown] = useState(null); // revealed text or null

  async function reveal() {
    if (shown !== null) {
      setShown(null);
      return;
    }
    setBusy(true);
    try {
      const r = await fetch(`/api/secrets/${encodeURIComponent(s.id)}/reveal`);
      if (!r.ok) throw new Error();
      const body = await r.json();
      setShown({ value: body.value || body.text || "", text: body.text || "" });
      setTimeout(() => setShown(null), 30000); // auto-hide
    } catch (e) {
      notify("Could not reveal (lockbox unreachable?).", "err");
    }
    setBusy(false);
  }

  async function del() {
    if (!(await confirmDialog(`Erase this lockbox secret (${s.label})? This can't be undone.`, { title: "Erase secret", confirmText: "Erase", danger: true }))) return;
    setBusy(true);
    const r = await send("forget_secret", { secret_id: s.id });
    setBusy(false);
    if (r.ok) { notify("Secret erased."); await reload(); }
    else notify(r.error || "Could not erase.", "err");
  }

  return html`<div class="mem-row">
    <div class="mem-main">
      <div class="mem-text">
        <span class="mono">🔒 ${s.label}</span>
      </div>
      ${shown !== null
        ? html`<div class="mono" style="margin-top:4px">${shown.value}</div>
          ${shown.text && shown.text !== shown.value
            ? html`<div style="font-size:12px;opacity:.7">“${shown.text}”</div>`
            : null}`
        : null}
      <div class="mem-meta micro">${new Date(s.created * 1000).toLocaleDateString()}
        ${s.source && s.source !== "voice" ? ` · via ${s.source}` : ""}</div>
    </div>
    ${controls
      ? html`<button class="btn-ghost" disabled=${busy} onClick=${reveal}>
            ${shown !== null ? "Hide" : "Reveal"}</button>
          <button class="btn-ghost danger" disabled=${busy} onClick=${del}>Forget</button>`
      : null}
  </div>`;
}

function daysLeft(expires) {
  if (!expires) return null;
  const d = (expires - Date.now() / 1000) / 86400;
  if (d <= 0) return "expiring";
  return d < 1.5 ? "expires in 1d" : `expires in ${Math.round(d)}d`;
}

function FactRow({ f, controls, reload, showOwner }) {
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(f.text);
  const [tier, setTier] = useState(f.tier);
  const [keep, setKeep] = useState("keep");  // keep | 30 | 90 | 365 | clear

  async function saveEdit() {
    const patch = { fact_id: f.id };
    if (text.trim() && text.trim() !== f.text) patch.text = text.trim();
    if (tier !== f.tier) patch.tier = tier;
    if (keep === "clear") patch.clear_expiry = true;
    else if (keep !== "keep") patch.expires_days = Number(keep);
    if (Object.keys(patch).length === 1) { setEditing(false); return; }
    setBusy(true);
    const r = await send("update_memory", patch);
    setBusy(false);
    if (r.ok) { setEditing(false); notify("Memory updated."); await reload(); }
    else notify(r.error || "Update failed.", "err");
  }

  async function review(action) {
    setBusy(true);
    const r = await send("review_memory", { fact_id: f.id, action });
    setBusy(false);
    if (r.ok) {
      notify(action === "vault" ? "Moved to the lockbox." : "Released.");
      await reload();
    } else notify(r.error || "Review failed.", "err");
  }

  async function del() {
    if (!(await confirmDialog(`Forget this? “${f.text}” — this can't be undone.`, { title: "Forget memory", confirmText: "Forget", danger: true }))) return;
    setBusy(true);
    const res = await send("forget_memory", { fact_id: f.id });
    setBusy(false);
    if (res.ok) {
      await reload();
      notify("Forgotten.");
    } else notify(res.error || "Delete failed.", "err");
  }

  if (editing) {
    return html`
      <div class="mem-row mem-edit">
        <div class="mem-main">
          <input class="mem-edit-text" value=${text}
            onInput=${(e) => setText(e.target.value)} />
          <div class="mem-edit-opts">
            <select class="audio-select" value=${tier} onChange=${(e) => setTier(e.target.value)}>
              <option value="private">Private — only they hear it</option>
              <option value="personal-public">About them — anyone recognized</option>
              <option value="shared">Shared — the whole household</option>
            </select>
            <select class="audio-select" value=${keep} onChange=${(e) => setKeep(e.target.value)}>
              <option value="keep">Retention: unchanged</option>
              <option value="clear">Keep forever</option>
              <option value="30">Forget after 30 days</option>
              <option value="90">Forget after 90 days</option>
              <option value="365">Forget after a year</option>
            </select>
          </div>
        </div>
        <button class="btn-ghost" disabled=${busy} onClick=${saveEdit}>${busy ? "…" : "Save"}</button>
        <button class="btn-ghost" disabled=${busy}
          onClick=${() => { setEditing(false); setText(f.text); setTier(f.tier); setKeep("keep"); }}>
          Cancel</button>
      </div>`;
  }
  return html`
    <div class="mem-row">
      <div class="mem-main">
        <div class="mem-text">${f.text}</div>
        <div class="mem-meta micro">
          <span class=${TIER_BADGE[f.tier] || "badge"}>${TIER_LABEL[f.tier] || f.tier}</span>
          ${f.state === "quarantined"
            ? html`<span class="badge warn" title="Awaiting classification — owner-only and invisible to models until resolved. Set memory.classifier_model to a local model to resolve these automatically.">held for review</span>`
            : null}
          ${showOwner ? `${f.owner_name || f.owner} · ` : ""}${ago(f.created)}
          ${f.source && f.source !== "voice" ? ` · via ${f.source}` : ""}
          ${daysLeft(f.expires) ? html` · <span class="badge warn">${daysLeft(f.expires)}</span>` : ""}
        </div>
      </div>
      ${controls && f.state === "quarantined"
        ? html`<button class="btn-ghost" disabled=${busy} onClick=${() => review("release")}>Release</button>
            <button class="btn-ghost" disabled=${busy} onClick=${() => review("vault")}>To lockbox</button>`
        : null}
      ${controls
        ? html`<button class="btn-ghost" disabled=${busy} onClick=${() => setEditing(true)}>Edit</button>
          <button class="btn-ghost danger" disabled=${busy}
            onClick=${del}>${busy ? "…" : "Forget"}</button>`
        : null}
    </div>`;
}
