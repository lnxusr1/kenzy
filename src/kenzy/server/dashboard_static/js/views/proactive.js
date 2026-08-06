// Proactive — what Kenzy has said on her own, and why she stayed quiet when she
// did. The refusals are the point: "why didn't she tell me about the leak?" is
// only answerable from a record that keeps the noes, and this page keeps them.
//
// It is shown even when the feature is switched OFF, deliberately. The spoken
// off-switch persists across restarts, so the failure worth guarding against is
// the house sitting silent for months with nobody aware. A tab that hid itself
// when disabled would hide exactly that.
import { html, useEffect, useState } from "../html.js";
import { getProactive } from "../api.js";
import { send } from "../store.js";

const AGO = (ts) => {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
};

export function ProactiveView() {
  const [d, setD] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  const load = async () => {
    try {
      setD(await getProactive());
      setErr("");
    } catch (e) {
      setErr(String(e));
    }
  };
  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);

  if (err) return html`<div class="empty">${err}</div>`;
  if (!d) return html`<div class="empty">Loading…</div>`;
  if (!d.available)
    return html`<div class="empty">Proactive speech needs the occupancy spine running —
      Home Assistant configured, and <span class="mono">occupancy.enabled</span> on.</div>`;

  const ro = !d.controls;
  const safetyOn = (d.categories || []).includes("safety");

  async function test() {
    setBusy(true);
    setNote("");
    const res = await send("test_proactive", {});
    setNote(res.ok ? "Spoken in every connected room." : `Not spoken — ${res.error}`);
    setBusy(false);
    load();
  }

  async function toggle(on) {
    setBusy(true);
    const res = await send("set_proactive_enabled", { enabled: on });
    if (!res.ok) setNote(res.error || "Could not change it.");
    setBusy(false);
    load();
  }

  return html`
    ${!d.enabled
      ? html`<div class="card pad node-alert">
          <strong>Kenzy will not speak up about anything.</strong>
          <p class="micro">Unprompted speech is switched off — including safety alerts.
            This survives restarts, so it stays off until it's turned back on here or by
            saying "turn on the alerts".</p>
          <button class="btn-primary" disabled=${ro || busy} onClick=${() => toggle(true)}>
            Turn it back on</button>
        </div>`
      : null}

    <section class="section">
      <div class="card pad">
        <dl class="kv">
          <dt>unprompted speech</dt>
          <dd>${d.enabled ? "on" : html`<strong>OFF</strong>`}</dd>
          <dt>safety alerts</dt>
          <dd>${safetyOn
            ? "on — smoke, carbon monoxide, gas, leaks, a triggered alarm panel"
            : html`off · turn on <span class="mono">proactive.safety.enabled</span> in Settings`}</dd>
          <dt>hazard sensors watched</dt>
          <dd>${d.watching}${d.watching === 0 && safetyOn
            ? html` — <span class="micro">nothing to announce; check Home Assistant → Safety sensors</span>`
            : ""}</dd>
          <dt>silenced right now</dt>
          <dd>${(d.silenced || []).length
            ? html`<span class="mono">${(d.silenced || []).join(", ")}</span>
                <span class="micro"> — silent until the sensor goes off and trips again</span>`
            : "nothing"}</dd>
        </dl>
        <div class="cfg-actions">
          <button class="btn-ghost" disabled=${ro || busy} onClick=${test}>
            ${busy ? "…" : "Test an alert"}</button>
          ${d.enabled
            ? html`<button class="btn-ghost danger" disabled=${ro || busy}
                onClick=${() => toggle(false)}>Turn off unprompted speech</button>`
            : null}
        </div>
        ${note ? html`<p class="micro">${note}</p>` : null}
        <p class="micro">A test goes through the same gate a real hazard does, so if it
          refuses, a real alarm would have been refused too — and it says why.</p>
      </div>
    </section>

    <section class="section">
      <h3>What she decided</h3>
      <p class="micro">Every decision, including the times she stayed quiet. Kept even when
        the log viewer is off — this is her own conduct, not household conversation.</p>
      ${!(d.log || []).length
        ? html`<div class="empty">Nothing yet.</div>`
        : html`<div class="card pad">
            ${(d.log || []).map(
              (r) => html`<div class="occ-row" key=${r.ts + r.key}>
                <span class="occ-name">${r.allowed ? "🔊" : "🔇"} ${r.text}</span>
                <span class="micro occ-where">${r.allowed ? "spoken" : r.reason}</span>
                <span class="mono occ-id">${r.key}</span>
                <span class="micro">${AGO(r.ts)}</span>
              </div>`,
            )}
          </div>`}
    </section>
  `;
}
