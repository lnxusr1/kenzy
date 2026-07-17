import { html, useState, useEffect } from "../html.js";
import { getPeople } from "../api.js";
import { send, notify } from "../store.js";

// People: everything voice-identity in one place. The model is person-first —
// a person can exist without a voice, but every enrolled voice belongs to a
// person (enrollment is started FOR a person, and the server adopts any voice
// enrolled by other paths). So the page is just people: add one, enroll their
// voice from a room, done. A "voices without a person" section appears only
// when legacy/CLI-enrolled orphans exist. Reads /api/people; mutations are
// save_person / delete_person / enroll_speaker / delete_speaker (controls-gated).
export function PeopleView() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");
  const [editing, setEditing] = useState(null); // person id, "new", or null
  const [form, setForm] = useState({ name: "", voiceprints: [] });
  const [enrolling, setEnrolling] = useState(null); // person id with the room picker open
  const [enrollRoom, setEnrollRoom] = useState("");

  const load = () =>
    getPeople()
      .then(setData)
      .catch((e) => setErr(String(e)));

  useEffect(() => {
    load();
  }, []);

  if (err) return html`<div class="empty">Could not load people: ${err}</div>`;
  if (!data) return html`<div class="empty">Loading…</div>`;

  const controls = data.controls;
  const people = data.people || [];
  const voiceprints = data.voiceprints || [];
  const rooms = data.rooms || [];
  const unassigned = voiceprints.filter((v) => !v.person_id);
  const voicesOf = (p) =>
    voiceprints.filter((v) => v.person_id === p.id);

  function startEdit(p) {
    setForm({ name: p.name, voiceprints: [...p.voiceprints] });
    setEditing(p.id);
    setEnrolling(null);
  }
  function startNew(seedVoice) {
    setForm({ name: "", voiceprints: seedVoice ? [seedVoice] : [] });
    setEditing("new");
    setEnrolling(null);
    if (seedVoice)
      // The assign buttons live below; the form opens at the top of the People
      // section — bring it into view so the click visibly did something.
      setTimeout(() => document.querySelector(".ppl-editing")?.scrollIntoView({ behavior: "smooth", block: "center" }), 50);
  }
  function cancel() {
    setEditing(null);
  }

  const toggleVoice = (name) =>
    setForm((f) => ({
      ...f,
      voiceprints: f.voiceprints.some((v) => v.toLowerCase() === name.toLowerCase())
        ? f.voiceprints.filter((v) => v.toLowerCase() !== name.toLowerCase())
        : [...f.voiceprints, name],
    }));

  async function save() {
    const name = form.name.trim();
    if (!name) return;
    const id = editing === "new" ? "" : editing;
    setBusy(id || "new");
    // person id rides as `person_id`: store.send() spreads the payload over the
    // envelope, and a payload `id` would clobber the request/ack correlation id.
    const res = await send("save_person", { person_id: id, name, voiceprints: form.voiceprints });
    setBusy("");
    if (res.ok) {
      setEditing(null);
      await load();
      notify(`Saved ${name}.`);
    } else notify(res.error || "Could not save.", "err");
  }

  async function delPerson(p) {
    const voices = voicesOf(p);
    const also = voices.length
      ? ` Their voice profile stays enrolled and will show below as unassigned.`
      : "";
    if (!window.confirm(`Delete “${p.name}”?${also}`)) return;
    setBusy(p.id);
    const res = await send("delete_person", { person_id: p.id });
    setBusy("");
    if (res.ok) {
      if (editing === p.id) setEditing(null);
      await load();
      notify(`Deleted ${p.name}.`);
    } else notify(res.error || "Delete failed.", "err");
  }

  async function delVoice(v) {
    if (!window.confirm(`Delete the voice profile “${v.name}”? This can't be undone.`)) return;
    setBusy("vp:" + v.name);
    const res = await send("delete_speaker", { name: v.name });
    setBusy("");
    if (res.ok) {
      await load();
      notify(`Deleted ${v.name}.`);
    } else notify(res.error || "Delete failed.", "err");
  }

  function openEnroll(p) {
    setEnrolling(p.id);
    setEnrollRoom(rooms[0] ? rooms[0].node_id : "");
    setEditing(null);
  }

  async function startEnroll(p) {
    const res = await send("enroll_speaker", { person_id: p.id, node: enrollRoom });
    if (res.ok) {
      setEnrolling(null);
      notify(`Enrolling ${p.name} in ${rooms.find((r) => r.node_id === enrollRoom)?.room || "the room"} — speak when prompted. The sample count updates when it finishes.`);
    } else notify(res.error || "Could not start enrollment.", "err");
  }

  // Voice summary line for a person card: "no voice yet" or "voice · N samples".
  const voiceSummary = (p) => {
    const voices = voicesOf(p);
    if (!voices.length) return "no voice yet";
    const samples = voices.reduce((n, v) => n + (v.samples || 0), 0);
    const label = voices.length === 1 ? "voice" : `${voices.length} voices`;
    return `${label} · ${samples} sample${samples === 1 ? "" : "s"}`;
  };

  // The edit form's voice picker: this person's voices + unowned ones. Other
  // people's voices aren't offered — enrollment is per-person now, so moving a
  // voice between people is no longer a first-class flow.
  const voicePicker = () => {
    const offer = voiceprints.filter(
      (v) => !v.person_id || v.person_id === editing,
    );
    if (voiceprints.length === 0)
      return html`<p class="micro">No voices are enrolled yet. Save, then use
        “Enroll voice” on the person's card.</p>`;
    if (offer.length === 0)
      return html`<p class="micro">Every enrolled voice already belongs to someone.
        Use “Enroll voice” on the card to record this person's voice.</p>`;
    return html`<div class="ppl-voices">
      ${offer.map((v) => {
        const on = form.voiceprints.some((x) => x.toLowerCase() === v.name.toLowerCase());
        return html`<label class="ha-chk" key=${v.name}>
          <input type="checkbox" checked=${on} onChange=${() => toggleVoice(v.name)} />
          <span class="mono">${v.name}</span>
        </label>`;
      })}
    </div>`;
  };

  const editForm = () => html`
    <div class="ppl-form">
      <label class="field">
        <span class="micro">Name</span>
        <input autofocus placeholder="e.g. Alice" value=${form.name}
          onInput=${(e) => setForm((f) => ({ ...f, name: e.target.value }))}
          onKeyDown=${(e) => {
            if (e.key === "Enter") save();
            if (e.key === "Escape") cancel();
          }} />
      </label>
      <div class="field">
        <span class="micro">Voices — which enrolled voices are this person</span>
        ${voicePicker()}
      </div>
      <div class="ppl-form-actions">
        <button class="btn-primary" disabled=${!form.name.trim() || busy} onClick=${save}>
          ${busy ? "Saving…" : "Save"}</button>
        <button class="btn-ghost" onClick=${cancel}>Cancel</button>
      </div>
    </div>`;

  const enrollRow = (p) => html`
    <div class="ppl-enroll-row">
      <span class="micro">Enroll from</span>
      <select value=${enrollRoom} onChange=${(e) => setEnrollRoom(e.target.value)}>
        ${rooms.map((r) => html`<option value=${r.node_id}>${r.room || r.node_id}</option>`)}
      </select>
      <button class="btn-primary" disabled=${!enrollRoom} onClick=${() => startEnroll(p)}>
        Start</button>
      <button class="btn-ghost" onClick=${() => setEnrolling(null)}>Cancel</button>
    </div>`;

  return html`
    <div class="stats">
      <div class="tile"><div class="micro">People</div><div class="k">${people.length}</div></div>
      <div class="tile"><div class="micro">Enrolled voices</div><div class="k">${voiceprints.length}</div></div>
      <div class="tile"><div class="micro">Unassigned voices</div><div class="k">${unassigned.length}</div></div>
    </div>

    ${!data.speaker_reachable
      ? html`<div class="banner warn">⚠ The speaker service isn't reachable, so enrolled voices
          can't be listed or managed. People still save, but check
          <span class="mono">speaker.url</span> and that
          <span class="mono">kenzy-speaker</span> is running.</div>`
      : null}

    <section class="section">
      <header><h2>People</h2><span class="rule"></span></header>
      <p class="micro">A person is a household member Kenzy knows by name. Add them, then
        enroll their voice from a room — re-enrolling later adds more samples, which makes
        recognition more reliable.</p>

      ${controls
        ? html`<div class="ppl-actions">
            <button class="btn-primary" disabled=${editing === "new"} onClick=${() => startNew()}>
              + Add person</button>
          </div>`
        : html`<p class="micro">Read-only — set <span class="mono">dashboard.controls: true</span>
            to add or edit people.</p>`}

      ${editing === "new"
        ? html`<div class="card ppl-card ppl-editing">
            <div class="ppl-head"><span class="ppl-name">New person</span></div>
            ${editForm()}
          </div>`
        : null}

      ${people.length === 0 && editing !== "new"
        ? html`<div class="empty">No people yet. ${controls
            ? "Add one above, then enroll their voice."
            : "Enable controls to add one."}</div>`
        : html`<div class="ppl-list">
            ${people.map((p) => {
              const isOpen = editing === p.id;
              return html`
                <div class=${"card ppl-card" + (isOpen ? " ppl-editing" : "")} key=${p.id}>
                  <div class="ppl-head">
                    <button class="ppl-title" disabled=${!controls}
                      onClick=${() => (isOpen ? cancel() : startEdit(p))}>
                      <span class="sk-chev" aria-hidden="true">${isOpen ? "▾" : "▸"}</span>
                      <span class="ppl-name">${p.name}</span>
                    </button>
                    ${!isOpen
                      ? html`<span class=${"ppl-owner" + (voicesOf(p).length ? "" : " ppl-unowned")}>
                          ${voiceSummary(p)}</span>`
                      : null}
                    ${controls
                      ? html`<div class="ppl-voice-actions">
                          <button class="btn-ghost" disabled=${!rooms.length || enrolling === p.id}
                            title=${rooms.length ? "" : "no room nodes connected"}
                            onClick=${() => openEnroll(p)}>Enroll voice</button>
                          <button class="btn-ghost danger" disabled=${busy === p.id}
                            onClick=${() => delPerson(p)}>${busy === p.id ? "…" : "Delete"}</button>
                        </div>`
                      : null}
                  </div>
                  ${enrolling === p.id ? enrollRow(p) : null}
                  ${isOpen ? editForm() : null}
                </div>`;
            })}
          </div>`}
    </section>

    ${unassigned.length && data.speaker_reachable
      ? html`<section class="section">
          <header><h2>Voices without a person</h2><span class="rule"></span></header>
          <p class="micro">Enrolled voices not linked to anyone (from an older setup or the
            ${" "}<span class="mono">kenzy-enroll</span> CLI). Assign each to a person — or delete it.</p>
          <div class="card ppl-voice-list">
            ${unassigned.map(
              (v) => html`
                <div class="ppl-voice-row" key=${v.name}>
                  <div class="ppl-voice">
                    <span class="mono">${v.name}</span>
                    <span class="micro">${v.samples} sample${v.samples === 1 ? "" : "s"}</span>
                  </div>
                  <div class="ppl-voice-actions">
                    <button class="btn-ghost" disabled=${!controls || editing === "new"}
                      onClick=${() => startNew(v.name)}>Assign to a person</button>
                    <button class="btn-ghost danger" disabled=${!controls || busy === "vp:" + v.name}
                      onClick=${() => delVoice(v)}>${busy === "vp:" + v.name ? "…" : "Delete"}</button>
                  </div>
                </div>`,
            )}
          </div>
        </section>`
      : null}`;
}
