import { html, useState, useEffect } from "../html.js";
import { getSpeakers } from "../api.js";
import { send, notify } from "../store.js";

// Speaker profile management: list enrolled voices with their sample counts and
// rename / delete them. Enrollment is voice-based (a room node captures audio), so
// the dashboard manages the list and can kick off enrollment on a connected node —
// it doesn't record audio in the browser.
export function SpeakersView() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");
  const [renaming, setRenaming] = useState(null); // {name, value}

  const load = () =>
    getSpeakers()
      .then(setData)
      .catch((e) => setErr(String(e)));

  useEffect(() => {
    load();
  }, []);

  if (err) return html`<div class="empty">Could not load speakers: ${err}</div>`;
  if (!data) return html`<div class="empty">Loading…</div>`;
  if (!data.reachable)
    return html`<div class="empty">The speaker service isn't reachable, so profiles can't be
      managed. Check <span class="mono">speaker.url</span> and that
      <span class="mono">kenzy-speaker</span> is running.</div>`;

  const controls = data.controls;
  const speakers = data.speakers || [];
  const rooms = data.rooms || [];

  async function del(name) {
    if (!window.confirm(`Delete the voice profile for “${name}”? This can't be undone.`)) return;
    setBusy(name);
    const res = await send("delete_speaker", { name });
    setBusy("");
    if (res.ok) {
      await load();
      notify(`Deleted ${name}.`);
    } else notify(res.error || "Delete failed.", "err");
  }

  async function rename() {
    const { name, value } = renaming;
    const next = (value || "").trim();
    if (!next || next === name) {
      setRenaming(null);
      return;
    }
    setBusy(name);
    const res = await send("rename_speaker", { name, new_name: next });
    setBusy("");
    setRenaming(null);
    if (res.ok) {
      await load();
      notify(`Renamed ${name} → ${next}.`);
    } else notify(res.error || "Rename failed.", "err");
  }

  return html`
    <div class="stats">
      <div class="tile"><div class="micro">Enrolled voices</div><div class="k">${speakers.length}</div></div>
      <div class="tile"><div class="micro">Identify threshold</div>
        <div class="k">${data.identify_threshold.toFixed(2)}</div></div>
    </div>

    <section class="section">
      <header><h2>Enrolled voices</h2><span class="rule"></span></header>
      ${!controls
        ? html`<p class="micro">Read-only — set <span class="mono">dashboard.controls: true</span>
            to rename or delete profiles.</p>`
        : null}
      ${speakers.length === 0
        ? html`<div class="empty">No voices enrolled yet. Use
            <span class="mono">kenzy-enroll</span> on the server, or enroll from a room below.</div>`
        : html`<div class="card">
            <div class="sk-list">
              ${speakers.map(
                (s) => html`
                  <div class="sk-row" key=${s.name}>
                    <div class="sk-main">
                      ${renaming && renaming.name === s.name
                        ? html`<input class="inline-edit" autofocus value=${renaming.value}
                            onInput=${(e) => setRenaming({ name: s.name, value: e.target.value })}
                            onKeyDown=${(e) => {
                              if (e.key === "Enter") rename();
                              if (e.key === "Escape") setRenaming(null);
                            }} />`
                        : html`<div class="sk-name"><span class="mono">${s.name}</span></div>`}
                      <div class="sk-desc micro">${s.samples} sample${s.samples === 1 ? "" : "s"}</div>
                    </div>
                    <div class="sk-meta">
                      ${renaming && renaming.name === s.name
                        ? html`<button class="btn-ghost" disabled=${busy === s.name}
                              onClick=${rename}>Save</button>
                            <button class="btn-ghost" onClick=${() => setRenaming(null)}>Cancel</button>`
                        : html`<button class="btn-ghost" disabled=${!controls}
                              onClick=${() => setRenaming({ name: s.name, value: s.name })}>Rename</button>
                            <button class="btn-ghost danger" disabled=${!controls || busy === s.name}
                              onClick=${() => del(s.name)}>${busy === s.name ? "…" : "Delete"}</button>`}
                    </div>
                  </div>
                `,
              )}
            </div>
          </div>`}
    </section>

    ${controls ? html`<${EnrollFromRoom} rooms=${rooms} />` : null}`;
}

// Secondary: start voice enrollment on a connected room node (no browser recording —
// the person speaks at that room's mic and Kenzy walks them through it).
function EnrollFromRoom({ rooms }) {
  const [name, setName] = useState("");
  const [node, setNode] = useState(rooms[0] ? rooms[0].node_id : "");

  async function start() {
    const res = await send("enroll_speaker", { name: name.trim(), node });
    if (res.ok) {
      notify(`Enrollment started in ${rooms.find((r) => r.node_id === node)?.room || "the room"} — speak when prompted.`);
      setName("");
    } else notify(res.error || "Could not start enrollment.", "err");
  }

  return html`
    <section class="section">
      <header><h2>Enroll from a room</h2><span class="rule"></span></header>
      ${rooms.length === 0
        ? html`<div class="empty">No room nodes are connected. Connect a node to enroll a voice,
            or use <span class="mono">kenzy-enroll</span> on the server.</div>`
        : html`<div class="card pad">
            <p class="micro">Kenzy will prompt the person at the chosen room to say a few
              sentences and enroll them. Use a unique name; enrolling an existing name adds
              more samples to it.</p>
            <div class="enroll-row">
              <input placeholder="Name (e.g. Alice)" value=${name}
                onInput=${(e) => setName(e.target.value)} />
              <select value=${node} onChange=${(e) => setNode(e.target.value)}>
                ${rooms.map((r) => html`<option value=${r.node_id}>${r.room || r.node_id}</option>`)}
              </select>
              <button class="btn-primary" disabled=${!name.trim() || !node} onClick=${start}>
                Start enrollment</button>
            </div>
          </div>`}
    </section>`;
}
