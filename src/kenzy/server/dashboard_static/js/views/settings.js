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

// Update check — installed version vs. the latest on PyPI. Read-only visibility
// layer; the actual upgrade action is a later, controls-gated step.
function UpdateCheck() {
  const [u, setU] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    fetch("/api/upgrade")
      .then((r) => (r.ok ? r.json() : { error: true }))
      .then(setU)
      .catch(() => setU({ error: true }));
  }, []);

  if (!u) return html`<p class="micro">Checking for updates…</p>`;
  if (u.error) return html`<p class="micro">Could not load the update status.</p>`;

  let status;
  if (!u.checkable) status = html`<span class="micro">couldn't reach PyPI</span>`;
  else if (u.current === "dev") status = html`<span class="micro">development build</span>`;
  else if (u.update_available)
    status = html`<span class="badge streaming">update available → ${u.latest}</span>`;
  else status = html`<span class="micro">up to date</span>`;

  async function upgrade() {
    if (
      !window.confirm(
        `Upgrade the server to ${u.latest} and restart it? The dashboard will disconnect ` +
          `while it installs (a few minutes) and reconnect when it's back. Your dependency ` +
          `pins (constraints.txt) are honored.`,
      )
    )
      return;
    setBusy(true);
    const res = await send("upgrade_server", { version: u.latest });
    setBusy(false);
    notify(
      res.ok
        ? "Upgrade started — installing in the background; watch for the result."
        : res.error || "Could not start the upgrade.",
      res.ok ? "ok" : "err",
    );
  }

  // Only offer the action when controls are on, an update exists, and this isn't a
  // dev/editable checkout. The backends/nodes upgrade separately (fan-out — later).
  const canUpgrade = u.controls && u.update_available && u.current !== "dev";

  return html`
    <dl class="kv">
      <dt>installed</dt><dd><span class="mono">${u.current}</span></dd>
      <dt>latest on PyPI</dt><dd><span class="mono">${u.latest || "—"}</span></dd>
      <dt>status</dt><dd>${status}</dd>
    </dl>
    ${canUpgrade
      ? html`<div class="cfg-actions">
            <button class="btn-primary" disabled=${busy} onClick=${upgrade}>
              ${busy ? "Starting…" : `Upgrade server to ${u.latest}`}</button>
          </div>
          <p class="micro">Upgrades this server host only (its <span class="mono">server</span>
            extra); restarts it on success. Backend services and nodes are upgraded
            separately.</p>`
      : null}`;
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

// Backup download with the two opt-in scope toggles. The link is a plain GET so
// the browser's download flow (and the session cookie) just work.
function BackupPanel() {
  const [secrets, setSecrets] = useState(false);
  const [full, setFull] = useState(false);
  const qs = [secrets ? "secrets=1" : "", full ? "full=1" : ""].filter(Boolean).join("&");
  return html`
    <p class="micro">
      Download the deployment's state — node/service settings, rooms, Home Assistant
      curation, <b>enrolled voice profiles</b>, dependency pins, and custom skills
      (state on the speaker/LLM hosts is fetched and merged automatically). Restore
      with ${" "}<span class="mono">kenzy-init --restore &lt;file&gt;</span>.
    </p>
    <label class="micro" style="display:block">
      <input type="checkbox" checked=${secrets} onChange=${(e) => setSecrets(e.target.checked)} />
      ${" "}Include secrets (<span class="mono">.env</span> / API keys) — the archive then
      contains <b>live credentials</b>; store it like a password.
    </label>
    <label class="micro" style="display:block">
      <input type="checkbox" checked=${full} onChange=${(e) => setFull(e.target.checked)} />
      ${" "}Include everything (adds <span class="mono">models/</span> — larger file;
      normally re-downloaded by <span class="mono">kenzy-setup</span>, but captures any
      hand-placed custom model on the server)
    </label>
    <p><a class="btn" href=${"/api/backup" + (qs ? "?" + qs : "")} download>Download backup</a></p>
  `;
}

// Write-only API-key entry (mirrors the change-password form): set a value, never
// read one back — the server only ever reports which names are set.
const KNOWN_KEYS = ["OPENAI_API_KEY", "HA_API_KEY", "HF_TOKEN"];

function ApiKeys({ envKeys, controls }) {
  const [name, setName] = useState(KNOWN_KEYS[0]);
  const [custom, setCustom] = useState("");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [setNames, setSetNames] = useState(envKeys);

  const allNames = [...new Set([...KNOWN_KEYS, ...setNames])];
  const effective = name === "__custom__" ? custom.trim() : name;

  async function submit(e) {
    e.preventDefault();
    if (!effective || !value) return;
    setBusy(true);
    const res = await send("set_secret", { name: effective, value });
    setBusy(false);
    if (res.ok) {
      setSetNames([...new Set([...setNames, effective])]);
      setValue("");
      notify(`${effective} saved — restart the affected services (Services tab) to apply.`);
    } else notify(res.error || "Could not save the key.", "err");
  }

  return html`
    <p class="micro">
      <b>Write-only:</b> values are stored in the server's <span class="mono">.env</span>
      and never shown again. Applies after the affected services restart. On a
      multi-host setup this writes the <i>server's</i> host only. Note the dashboard
      runs over plain HTTP — enter keys from a machine on your own network.
    </p>
    <dl class="kv">
      ${allNames.map(
        (k) => html`<dt><span class="mono">${k}</span></dt>
          <dd>${setNames.includes(k)
            ? html`<span class="badge streaming">set</span>`
            : html`<span class="micro">not set</span>`}</dd>`,
      )}
    </dl>
    ${controls
      ? html`<form onSubmit=${submit} class="enroll-row">
          <select value=${name} onChange=${(e) => setName(e.target.value)}>
            ${allNames.map((k) => html`<option value=${k}>${k}</option>`)}
            <option value="__custom__">Other…</option>
          </select>
          ${name === "__custom__"
            ? html`<input placeholder="NAME_LIKE_THIS" value=${custom}
                onInput=${(e) => setCustom(e.target.value)} />`
            : null}
          <input type="password" placeholder="value (write-only)" value=${value}
            onInput=${(e) => setValue(e.target.value)} autocomplete="off" />
          <button class="btn" disabled=${busy || !effective || !value}>
            ${busy ? "…" : "Set"}
          </button>
        </form>`
      : html`<p class="micro">Read-only — set
          ${" "}<span class="mono">dashboard.controls: true</span> to set keys.</p>`}
  `;
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
        <header><h2>Updates</h2><span class="rule"></span></header>
        <div class="card pad"><${UpdateCheck} /></div>
      </section>

      <section class="section">
        <header><h2>Backup</h2><span class="rule"></span></header>
        <div class="card pad"><${BackupPanel} /></div>
      </section>

      <section class="section">
        <header><h2>API keys</h2><span class="rule"></span></header>
        <div class="card pad">
          <${ApiKeys} envKeys=${s.env_keys || []} controls=${s.flags && s.flags.controls} />
        </div>
      </section>

      <section class="section">
        <header><h2>Node provisioning</h2><span class="rule"></span></header>
        <div class="card pad">
          ${s.join_token
            ? html`<p class="micro">Join token — add a new room node with it:
                  ${" "}<code class="mono">kenzy-init --profile node --token …</code> (or the
                  installer's <code class="mono">--token</code>). It must match on every node.</p>
                <${CopyField} value=${s.join_token} />`
            : html`<p class="micro">⚠ No join token is set, so any device on the network can
                register as a node and read service config. Set
                ${" "}<code class="mono">discovery.token</code> in server.yaml (or re-run
                ${" "}<code class="mono">kenzy-init</code>) to require one.</p>`}
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
