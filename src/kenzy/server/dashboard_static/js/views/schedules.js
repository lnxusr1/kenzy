import { html, useState, useEffect } from "../html.js";
import { getSchedules } from "../api.js";
import { send, notify } from "../store.js";

// Scheduled view: the active timers / alarms / reminders held by the server's
// scheduler, with a live countdown and per-entry Cancel. Deliberately simple —
// entries are set by voice; this is the operator's visibility + cancel surface.

const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

function fmtDays(days) {
  if (!days || days.length === 0) return "";
  const ordered = DAYS.filter((d) => days.includes(d));
  if (ordered.length === 7) return "every day";
  if (ordered.join() === DAYS.slice(0, 5).join()) return "every weekday";
  if (ordered.join() === DAYS.slice(5).join()) return "every weekend";
  return "every " + ordered.map((d) => DAY_FULL[DAYS.indexOf(d)]).join(", ");
}

function fmtClock(at) {
  const h = parseInt(at.slice(0, 2), 10);
  return `${h % 12 || 12}:${at.slice(3)} ${h < 12 ? "AM" : "PM"}`;
}

function fmtLeft(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s % 60}s`;
  return `${s}s`;
}

const KIND_ICO = { timer: "⏳", alarm: "⏰", reminder: "✎", command: "▶" };

export function SchedulesView() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");
  const [now, setNow] = useState(Date.now());

  const load = () =>
    getSchedules()
      .then(setData)
      .catch((e) => setErr(String(e)));

  useEffect(() => {
    load();
    const tick = setInterval(() => setNow(Date.now()), 1000);
    const refresh = setInterval(load, 30000); // catch fired/voice-set entries
    return () => {
      clearInterval(tick);
      clearInterval(refresh);
    };
  }, []);

  if (err) return html`<div class="empty">Could not load schedules: ${err}</div>`;
  if (!data) return html`<div class="empty">Loading…</div>`;

  const entries = data.schedules || [];
  const controls = data.controls;
  const counts = { timer: 0, alarm: 0, reminder: 0, command: 0 };
  entries.forEach((e) => {
    counts[e.kind] = (counts[e.kind] || 0) + 1;
  });

  async function cancel(e) {
    setBusy(e.id);
    const res = await send("cancel_schedule", { sid: e.id });
    setBusy("");
    if (res.ok) {
      await load();
      notify("Cancelled.");
    } else notify(res.error || "Cancel failed.", "err");
  }

  return html`
    <div class="stats">
      <div class="tile"><div class="micro">Timers</div><div class="k">${counts.timer}</div></div>
      <div class="tile"><div class="micro">Alarms</div><div class="k">${counts.alarm}</div></div>
      <div class="tile"><div class="micro">Reminders</div><div class="k">${counts.reminder}</div></div>
      <div class="tile"><div class="micro">Commands</div><div class="k">${counts.command}</div></div>
    </div>

    <section class="section">
      <header><h2>Scheduled</h2><span class="rule"></span></header>
      ${!controls
        ? html`<p class="micro">Read-only — set <span class="mono">dashboard.controls: true</span>
            to cancel entries.</p>`
        : null}
      ${entries.length === 0
        ? html`<div class="empty">Nothing scheduled. Set one by voice — “set a timer for ten
            minutes”, “wake me at seven”, “remind me at six to take out the trash”.</div>`
        : html`<div class="card">
            <div class="sk-list">
              ${entries.map((e) => {
                const when = e.at
                  ? `${fmtClock(e.at)}${e.days && e.days.length ? " " + fmtDays(e.days) : ""}`
                  : `in ${fmtLeft(e.fire_at * 1000 - now)}`;
                return html`
                  <div class="sk-row" key=${e.id}>
                    <div class="sk-main">
                      <div class="sk-name">
                        <span>${KIND_ICO[e.kind] || "•"} ${e.kind}</span>
                        ${e.label ? html` <span class="mono">${e.label}</span>` : null}
                      </div>
                      <div class="sk-desc micro">
                        ${when} · ${e.room || e.node_id}
                        ${!e.at ? html` · fires ${new Date(e.fire_at * 1000).toLocaleTimeString()}` : null}
                      </div>
                    </div>
                    <div class="sk-meta">
                      <button class="btn-ghost danger" disabled=${!controls || busy === e.id}
                        onClick=${() => cancel(e)}>${busy === e.id ? "…" : "Cancel"}</button>
                    </div>
                  </div>
                `;
              })}
            </div>
          </div>`}
    </section>
  `;
}
