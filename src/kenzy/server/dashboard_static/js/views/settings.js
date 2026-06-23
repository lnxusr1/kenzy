import { html, useState, useEffect } from "../html.js";
import { getSettings } from "../api.js";
import { send, notify } from "../store.js";

// A read-only secret with a copy button (join/API token). Shown only on the
// auth-gated Settings page so the operator can copy it instead of memorizing it.
function CopyField({ value }) {
  const [copied, setCopied] = useState(false);
  if (!value) return null;
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      notify("Copy failed — select the value and copy manually.", "err");
    }
  };
  return html`<div class="token-row">
    <code class="mono token-val">${value}</code>
    <button class="btn-ghost" onClick=${copy}>${copied ? "Copied ✓" : "Copy"}</button>
  </div>`;
}

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
        ${s.default_password
          ? html`<div class="banner warn">
              ⚠ This dashboard is still using the <b>default password</b>
              (<code class="mono">admin/password</code>). Anyone who can reach it can take
              control — change it below.
            </div>`
          : null}
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
        <header><h2>Node provisioning</h2><span class="rule"></span></header>
        <div class="card pad">
          ${s.join_token
            ? html`<p class="micro">Join token — add a new room node with it:
                  <code class="mono">kenzy-init --profile node --token …</code> (or the
                  installer's <code class="mono">--token</code>). It must match on every node.</p>
                <${CopyField} value=${s.join_token} />`
            : html`<p class="micro">⚠ No join token is set, so any device on the network can
                register as a node and read service config. Set
                <code class="mono">discovery.token</code> in server.yaml (or re-run
                <code class="mono">kenzy-init</code>) to require one.</p>`}
          ${s.api_token
            ? html`<p class="micro" style="margin-top:var(--s4)">API/CLI bearer
                  (<code class="mono">dashboard.auth_token</code>):</p>
                <${CopyField} value=${s.api_token} />`
            : null}
        </div>
      </section>

      <section class="section">
        <header><h2>Server configuration</h2><span class="rule"></span></header>
        <div class="card pad"><${ServerSettings} /></div>
      </section>
    </div>`;
}
