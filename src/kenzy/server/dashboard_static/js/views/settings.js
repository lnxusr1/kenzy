import { html, useState, useEffect } from "../html.js";
import { getSettings } from "../api.js";
import { send, notify } from "../store.js";

function Flag({ on, label, note }) {
  return html`
    <div class=${"flagrow " + (on ? "on" : "off")}>
      <span class=${"led " + (on ? "up" : "down")}></span>
      <div class="flagtext">
        <b>${label}</b>
        <span class="micro">${on ? "enabled" : "disabled"} — ${note}</span>
      </div>
    </div>`;
}

function ChangePassword({ username, onChanged }) {
  const [cur, setCur] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function submit(e) {
    e.preventDefault();
    setErr("");
    if (next !== confirm) {
      setErr("New passwords do not match.");
      return;
    }
    if (next.length < 4) {
      setErr("New password must be at least 4 characters.");
      return;
    }
    setBusy(true);
    const res = await send("set_password", { current: cur, new: next });
    setBusy(false);
    if (res.ok) {
      notify("Password changed — please sign in again.");
      onChanged();
    } else {
      setErr(res.error || "Could not change the password.");
    }
  }

  return html`
    <form class="pw-form" onSubmit=${submit}>
      <p class="micro">
        Updates <span class="mono">dashboard.auth</span> in server.yaml and takes effect
        immediately. You will be signed out of other sessions.
      </p>
      <div class="field">
        <label>Current password</label>
        <input type="password" value=${cur} autocomplete="current-password"
               onInput=${(e) => setCur(e.target.value)} />
      </div>
      <div class="field">
        <label>New password</label>
        <input type="password" value=${next} autocomplete="new-password"
               onInput=${(e) => setNext(e.target.value)} />
      </div>
      <div class="field">
        <label>Confirm new password</label>
        <input type="password" value=${confirm} autocomplete="new-password"
               onInput=${(e) => setConfirm(e.target.value)} />
      </div>
      ${err ? html`<p class="cfg-msg err">${err}</p>` : null}
      <button class="btn-primary" disabled=${busy || !cur || !next}>
        ${busy ? "Saving…" : `Change password for “${username || "admin"}”`}</button>
    </form>`;
}

export function SettingsView({ onLogout }) {
  const [info, setInfo] = useState(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    getSettings()
      .then(setInfo)
      .catch((e) => setErr(String(e)));
  }, []);

  if (err) return html`<div class="empty">Could not load settings: ${err}</div>`;
  if (!info) return html`<div class="empty">Loading settings…</div>`;

  const s = info;
  const kv = (k, v) => html`<dt>${k}</dt><dd>${v}</dd>`;

  return html`
    <div class="settings">
      <section class="section">
        <header><h2>Account</h2><span class="rule"></span></header>
        <div class="card pad">
          ${s.can_set_password
            ? html`<${ChangePassword} username=${s.username} onChanged=${onLogout} />`
            : html`<div class="banner">
                Password changes are unavailable because server.yaml could not be located.
                Use <code class="mono">kenzy-passwd</code> on the server host instead.
              </div>`}
        </div>
      </section>

      <section class="section">
        <header><h2>System</h2><span class="rule"></span></header>
        <div class="card pad">
          <dl class="kv">
            ${kv("kenzy version", html`<span class="mono">${s.version}</span>`)}
            ${kv("signed in as", html`<span class="mono">${s.username || "—"}</span>`)}
            ${kv("server (node WS)", html`<span class="mono">${s.server.host}:${s.server.port}</span>`)}
            ${kv("dashboard", html`<span class="mono">${s.dashboard.bind}:${s.dashboard.port}</span>`)}
            ${kv(
              "mDNS discovery",
              s.discovery.enabled
                ? html`<span class="mono">${s.discovery.instance}</span>${s.discovery.auth_required
                    ? html` · <span class="badge streaming">token required</span>`
                    : ""}`
                : "off",
            )}
          </dl>
        </div>
      </section>

      <section class="section">
        <header><h2>Feature flags</h2><span class="rule"></span></header>
        <div class="card pad flags">
          <${Flag} on=${s.flags.controls} label="Controls"
            note="config edits, rename, trigger/stop/restart, announce" />
          <${Flag} on=${s.flags.logs} label="Logs"
            note="pull node/service log buffers into the Logs tab" />
          <${Flag} on=${s.flags.tuning} label="Tuning"
            note="reserved for future live-tuning controls" />
          <p class="micro">
            Flags are set under <span class="mono">dashboard</span> in server.yaml and require a
            server restart to change.
          </p>
        </div>
      </section>

      <section class="section">
        <header><h2>Backend services</h2><span class="rule"></span></header>
        ${s.services.length
          ? html`<div class="card pad">
              <dl class="kv">
                ${s.services.map((svc) => kv(svc.name, html`<span class="mono">${svc.url}</span>`))}
              </dl>
            </div>`
          : html`<div class="empty">No backend services configured.</div>`}
      </section>
    </div>`;
}
