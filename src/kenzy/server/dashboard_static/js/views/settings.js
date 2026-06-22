import { html, useState, useEffect } from "../html.js";
import { getSettings } from "../api.js";
import { send, notify } from "../store.js";

// Editable server settings (the safe subset; written to server.local.yaml and applied
// by restarting the server). Lockout/secret-risky keys stay file/CLI-managed.
function ServerSettings() {
  const [data, setData] = useState(null);
  const [vals, setVals] = useState({});
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    fetch("/api/server/config")
      .then((r) => (r.ok ? r.json() : { error: true }))
      .then((d) => {
        setData(d);
        if (d.fields) {
          const v = {};
          for (const f of d.fields) v[f.key] = f.value;
          setVals(v);
        }
      })
      .catch(() => setData({ error: true }));
  }, []);

  if (!data) return html`<div class="empty">Loading…</div>`;
  if (data.error) return html`<div class="banner">Could not load server settings.</div>`;
  if (!data.writable)
    return html`<div class="banner">server.yaml location is unknown, so settings can't be
      edited here. Edit server.yaml on the host instead.</div>`;

  const set = (k, v) => setVals((cur) => ({ ...cur, [k]: v }));

  async function save() {
    const patch = {};
    for (const f of data.fields) {
      let v = vals[f.key];
      if (f.type === "num") {
        if (v === "" || v == null) continue;
        const num = Number(v);
        if (Number.isNaN(num)) continue;
        v = num;
      } else if (f.type === "bool") v = !!v;
      else v = v ?? "";
      if (JSON.stringify(v) !== JSON.stringify(f.value)) patch[f.key] = v;
    }
    if (!Object.keys(patch).length) {
      notify("No changes to save.");
      return;
    }
    if (!window.confirm("Save and restart the server now? The dashboard will briefly disconnect."))
      return;
    setBusy(true);
    const res = await send("set_server_config", { config: patch });
    setBusy(false);
    notify(
      res.ok ? "Saved — server restarting; the dashboard will reconnect." : res.error || "Save failed.",
      res.ok ? "ok" : "err",
    );
  }

  const row = (f) => {
    const v = vals[f.key];
    let input;
    if (f.type === "bool")
      input = html`<select onChange=${(e) => set(f.key, e.target.value === "true")}>
        <option value="true" selected=${v === true}>on</option>
        <option value="false" selected=${v !== true}>off</option></select>`;
    else if (f.type === "num")
      input = html`<input type="number" step="any" inputmode="decimal" value=${v ?? ""}
        onInput=${(e) => set(f.key, e.target.value)} />`;
    else
      input = html`<input value=${v ?? ""} placeholder=${f.value == null ? "unset" : ""}
        onInput=${(e) => set(f.key, e.target.value)} />`;
    return html`<div class=${"cfg-row" + (f.overridden ? " overridden" : "")}>
      <div class="cfg-key"><span class="mono">${f.key}</span>
        <span class="micro">${f.overridden ? "overridden" : "server.yaml"}</span></div>
      <div class="cfg-input">${input}</div></div>`;
  };

  return html`
    <p class="micro">Written to <span class="mono">server.local.yaml</span> (layered over
      server.yaml) and applied by <b>restarting the server</b>. Bind/port, login, and the
      discovery token stay file/CLI-managed for safety.</p>
    <div class="cfg-grid">${data.fields.map(row)}</div>
    <div class="cfg-actions">
      <button class="btn-primary" disabled=${busy} onClick=${save}>
        ${busy ? "Saving…" : "Save & restart server"}</button>
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
        <header><h2>Server configuration</h2><span class="rule"></span></header>
        <div class="card pad"><${ServerSettings} /></div>
      </section>
    </div>`;
}
