import { html, useState, useEffect } from "../html.js";
import { confirmDialog } from "../dialog.js";
import { groupBySections, serverHelp, SERVER_SECTIONS } from "../schema.js";
import { getServerFeatures, getServerUnit, getSettings } from "../api.js";
import { send, notify, subscribeUpgrades, useFleet } from "../store.js";

// Code defaults the server applies when a key is set in neither server.yaml nor
// the override — shown as the placeholder so an unset field still tells you what
// happens (mirrors the node editor's DEFAULTS map). Keep in sync with the
// cfg.get() fallbacks in server.py / integrations.
const CODE_DEFAULTS = {
  experimental: false,
  "dashboard.logs": true,
  "dashboard.controls": true,
  "stt.timeout": 60,
  "tts.timeout": 60,
  "llm.timeout": 30,
  "speaker.timeout": 10,
  "dialog.max_turns": 6,
  "alarm.ring_repeats": 10,
  "alarm.ring_interval": 25,
  "discovery.enabled": true,
  "discovery.instance": "kenzy-server",
  "integrations.mqtt.enabled": false,
  "integrations.mqtt.host": "127.0.0.1",
  "integrations.mqtt.port": 1883,
  "integrations.mqtt.base_topic": "kenzy",
  "integrations.mqtt.discovery_prefix": "homeassistant",
  "integrations.mqtt.commands": true,
  "streaming.enabled": true,
};

// Inherited value → human placeholder/label text.
const fmt = (v) =>
  v === undefined
    ? "unset"
    : v === null
      ? "null"
      : typeof v === "boolean"
        ? v
          ? "on"
          : "off"
        : String(v);

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
    // Node-editor semantics: a field's value is the override layer only; an
    // emptied field sends null, which removes the key from server.local.yaml
    // (reverting to the server.yaml / code default shown in its placeholder).
    const patch = {};
    for (const f of data.fields) {
      let v = vals[f.key];
      if (v === "" || v === undefined) v = null;
      if (v !== null && f.type === "num") {
        const num = Number(v);
        if (Number.isNaN(num)) continue; // unparseable input = no change
        v = num;
      }
      const before = f.value === undefined ? null : f.value;
      if (JSON.stringify(v) !== JSON.stringify(before)) patch[f.key] = v;
    }
    if (!Object.keys(patch).length) {
      notify("No changes to save.");
      return;
    }
    if (!(await confirmDialog("Save and restart the server now? The dashboard will briefly disconnect.", { title: "Save & restart", confirmText: "Save & restart" })))
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
    const isSet = v !== null && v !== undefined && v !== "";
    const inh = f.inherited !== null && f.inherited !== undefined ? f.inherited : CODE_DEFAULTS[f.key];
    let input;
    if (f.type === "bool")
      input = html`<select onChange=${(e) => set(f.key, e.target.value === "" ? null : e.target.value === "true")}>
        <option value="" selected=${!isSet}>inherit (${fmt(inh)})</option>
        <option value="true" selected=${v === true}>on</option>
        <option value="false" selected=${v === false}>off</option></select>`;
    else if (f.type === "num")
      input = html`<input type="number" step="any" inputmode="decimal" value=${isSet ? v : ""}
        placeholder=${fmt(inh)} onInput=${(e) => set(f.key, e.target.value)} />`;
    else
      input = html`<input value=${isSet ? v : ""} placeholder=${fmt(inh)}
        onInput=${(e) => set(f.key, e.target.value)} />`;
    return html`<div class=${"cfg-row" + (isSet ? " overridden" : "")}>
      <div class="cfg-key"><span class="mono">${f.key}</span>
        ${serverHelp(f.key) ? html`<span class="cfg-help">${serverHelp(f.key)}</span>` : null}</div>
      <div class="cfg-input">${input}</div></div>`;
  };

  return html`
    <p class="micro">Written to <span class="mono">server.local.yaml</span> (layered over
      server.yaml) and applied by <b>restarting the server</b>. Bind/port, login, and the
      discovery token stay file/CLI-managed for safety.</p>
    <div class="cfg-grid">${groupBySections(data.fields, SERVER_SECTIONS, (f) => f.key).map(
      ([label, fields]) => html`<div class="cfg-group">
        <div class="cfg-group-head mono">${label}</div>${fields.map(row)}</div>`,
    )}</div>
    <div class="cfg-actions">
      <button class="btn-primary" disabled=${busy} onClick=${save}>
        ${busy ? "Saving…" : "Save & restart"}</button>
    </div>`;
}

// Optional server-process extras (mqtt, sound) — the same chip language as
// the service editors, with a per-chip Install (dependency fill, constraints
// honored; the server restarts itself to load the new import).
function ServerFeatures({ controls }) {
  const [feats, setFeats] = useState(null);
  const [installing, setInstalling] = useState(false);

  const load = () => getServerFeatures().then((d) => setFeats(d.features || [])).catch(() => setFeats([]));
  useEffect(() => { load(); }, []);

  if (!feats || !feats.length) return null;
  return html`<div class="feat-chips">
    ${feats.map((f) => {
      const state = !f.available ? "missing" : f.active ? "active" : f.configured ? "inactive" : "off";
      const label =
        state === "missing"
          ? f.configured
            ? "enabled in config — NOT INSTALLED"
            : "not installed"
          : state === "active"
            ? "active"
            : state === "inactive"
              ? "installed — needs restart or config"
              : "available, not enabled";
      return html`<div key=${f.name} class=${"chip feat " + state} title=${f.note || ""}>
        <span class="mono">${f.name}</span>
        <span class="micro">${label}</span>
        ${state === "missing" && f.install === "pip" && controls
          ? html`<button class="btn-ghost" disabled=${installing}
              onClick=${async () => {
                setInstalling(true);
                notify(`Installing the ${f.name} extra — the server restarts itself when done.`);
                const r = await send("install_server_deps", { extra: f.extra }, 620000);
                setInstalling(false);
                notify(r.ok ? "Installed — server restarting." : r.error || "Install failed.", r.ok ? "ok" : "err");
                if (r.ok) setTimeout(load, 6000);
              }}>${installing ? "Installing…" : "Install"}</button>`
          : null}
      </div>`;
    })}
  </div>`;
}

// Server controls — mirrors each service page's Controls section. Restart
// re-execs in place; Disable is the systemd-install "off until I say so"
// (`disable --now`, so Restart= can't resurrect it). No Enable twin on
// purpose: a disabled server has no dashboard — the recovery one-liner is
// shown BEFORE you pull the plug.
function ServerControls({ controls }) {
  const [unit, setUnit] = useState(null);

  useEffect(() => {
    getServerUnit().then(setUnit).catch(() => setUnit(null));
  }, []);

  async function restart() {
    if (!(await confirmDialog("Restart the server now? The dashboard will briefly disconnect.", { title: "Restart server", confirmText: "Restart" })))
      return;
    const res = await send("restart_server", {});
    notify(
      res.ok ? "Server restarting — the dashboard will reconnect." : res.error || "Restart failed.",
      res.ok ? "ok" : "err",
    );
  }

  async function disable() {
    if (!(await confirmDialog(
      `Disable and stop the whole server? Every room goes quiet and this dashboard goes away until you run  systemctl --user enable --now ${unit.unit}  on the server host.`,
      { title: "Disable server", confirmText: "Disable", danger: true, typed: "DISABLE" },
    )))
      return;
    const res = await send("disable_server", {});
    notify(res.ok ? "Server disabling — goodbye." : res.error || "Disable failed.", res.ok ? "ok" : "err");
  }

  return html`
    <div class="ctl-row">
      <button class="btn-ghost danger" disabled=${!controls} onClick=${restart}>Restart</button>
      ${unit && unit.systemd && unit.exists
        ? html`<button class="btn-ghost danger" disabled=${!controls} onClick=${disable}>Disable server</button>`
        : null}
    </div>
    ${unit && unit.systemd && unit.exists
      ? html`<p class="micro">Unit <span class="mono">${unit.unit}</span> ·
          ${unit.enabled ? "enabled" : "disabled"} · ${unit.active ? "running" : "stopped"}.
          Disabling stops everything until re-enabled on the server host.</p>`
      : html`<p class="micro">Not running as a <span class="mono">systemd --user</span> unit —
          enable/disable doesn't apply here (dev checkout or manual start).</p>`}`;
}

// Update check — installed version vs. the latest on PyPI. Read-only visibility
// layer; the actual upgrade action is a later, controls-gated step.
function UpdateCheck() {
  const [u, setU] = useState(null);
  const [busy, setBusy] = useState(false);
  const [busyAll, setBusyAll] = useState(false);
  const [log, setLog] = useState([]); // running per-item log of an upgrade pass
  const { live, data: fleetData } = useFleet();
  // Re-fetch whenever the live channel (re)connects — after step 1's server
  // restart drops the WS, the fresh state is what arms step 2.
  useEffect(() => {
    if (live === false && u) return; // keep showing the last state while down
    fetch("/api/upgrade")
      .then((r) => (r.ok ? r.json() : { error: true }))
      .then(setU)
      .catch(() => setU({ error: true }));
  }, [live]);
  useEffect(
    () =>
      subscribeUpgrades((m) => {
        const who = m.target || "server";
        if (m.type === "upgrade_progress")
          setLog((l) => [
            ...l,
            { text: `${m.step ? `[${m.step}/${m.total}] ` : ""}${who} — installing…` },
          ]);
        else if (m.type === "upgrade_result")
          setLog((l) => [
            ...l,
            {
              ok: m.ok,
              text: `${who} — ${m.ok ? m.output || "done" : "FAILED: " + ((m.output || "").trim().split("\n").pop() || "see logs")}`,
            },
          ]);
        else if (m.type === "upgrade_all_done") {
          setLog((l) => [...l, { ok: m.ok, text: `finished: ${m.summary || "done"}` }]);
          setBusyAll(false);
        }
      }),
    [],
  );

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
      !(await confirmDialog(
        `Upgrade the server to ${u.latest} and restart it? The dashboard will disconnect ` +
          `while it installs (a few minutes) and reconnect when it's back. Your dependency ` +
          `pins (constraints.txt) are honored.`,
        { title: "Upgrade server", confirmText: "Upgrade" },
      ))
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

  async function upgradeAll() {
    if (
      !(await confirmDialog(
        `Upgrade every backend service and node${u.latest ? ` to ${u.latest}` : ""}? They run ` +
          `one at a time (services first, then nodes) with a running log below. Anything ` +
          `already holding the new version is simply restarted. Run this AFTER upgrading ` +
          `the server.`,
        { title: "Upgrade everything", confirmText: "Upgrade all" },
      ))
    )
      return;
    setLog([]);
    setBusyAll(true);
    const res = await send("upgrade_all", { version: u.latest });
    if (!res.ok) {
      setBusyAll(false);
      notify(res.error || "Could not start the upgrade.", "err");
    }
  }

  // Logical progression: step 1 (server) only when an update exists and this
  // isn't a dev/editable checkout; step 2 (services + nodes) shows only while
  // something in the fleet still needs it — a running version behind `latest`,
  // an installed-but-not-restarted service, or an unknown version we can't
  // vouch for. Once everything is current it disappears, like step 1 does.
  // It stays DISABLED until the server itself is current (upgrade order).
  const vnum = (v) => String(v).split(".").slice(0, 4).map((s) => parseInt(s, 10) || 0);
  const isNewer = (a, b) => {
    const A = vnum(a), B = vnum(b);
    for (let i = 0; i < Math.max(A.length, B.length); i++) {
      const d = (A[i] || 0) - (B[i] || 0);
      if (d) return d > 0;
    }
    return false;
  };
  const fleetBehind = (() => {
    if (!u.latest) return false;
    const services = (fleetData?.services || []).filter((s) => s.up);
    const nodes = (fleetData?.nodes || []).filter((n) => n.connected);
    const svcBehind = services.some((s) => {
      const d = s.detail || {};
      if (d.version === "dev") return false; // editable checkout — not upgradable
      return !d.version || isNewer(u.latest, d.version) ||
        (d.installed && d.installed !== d.version); // upgraded on disk, restart owed
    });
    const nodeBehind = nodes.some(
      (n) => n.version !== "dev" && (!n.version || isNewer(u.latest, n.version)),
    );
    return svcBehind || nodeBehind;
  })();
  const serverBehind = u.update_available && u.current !== "dev";
  const canUpgrade = u.controls && serverBehind;
  const canUpgradeAll = u.controls && u.checkable && (fleetBehind || serverBehind || busyAll);

  return html`
    <dl class="kv">
      <dt>installed</dt><dd><span class="mono">${u.current}</span></dd>
      <dt>latest on PyPI</dt><dd><span class="mono">${u.latest || "—"}</span></dd>
      <dt>status</dt><dd>${status}</dd>
    </dl>
    ${canUpgrade || canUpgradeAll
      ? html`<div class="cfg-actions">
            ${canUpgrade
              ? html`<button class="btn-primary" disabled=${busy} onClick=${upgrade}>
                  ${busy ? "Starting…" : `1. Upgrade server to ${u.latest}`}</button>`
              : null}
            ${canUpgradeAll
              ? html`<button class="btn-primary" disabled=${busyAll || serverBehind}
                  title=${serverBehind ? "Upgrade the server first — this step arms once it's current." : ""}
                  onClick=${upgradeAll}>
                  ${busyAll ? "Upgrading…" : `${serverBehind ? "2. " : ""}Upgrade services + nodes`}</button>`
              : null}
          </div>
          <p class="micro">Two steps: the server first (restarts itself on success), then
            everything else in one sequential pass — components already holding the new
            version just restart. Dependency pins (constraints.txt) are honored.</p>`
      : null}
    ${log.length
      ? html`<div class="upgrade-log mono">
          ${log.map(
            (e) => html`<div class=${e.ok === false ? "err" : e.ok ? "ok" : ""}>${e.text}</div>`,
          )}
        </div>`
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
  const [lbKey, setLbKey] = useState(true);
  const qs = [secrets ? "secrets=1" : "", full ? "full=1" : "", lbKey ? "" : "lockbox_key=0"]
    .filter(Boolean).join("&");
  return html`
    <p class="micro">
      Download the deployment's state — node/service settings, rooms, Home Assistant
      curation, <b>enrolled voice profiles</b>, dependency pins, and custom skills
      (state on the speaker/LLM hosts is fetched and merged automatically). Restore
      with ${" "}<span class="mono">kenzy-init --restore ${"<file>"}</span>.
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
    <label class="micro" style="display:block">
      <input type="checkbox" checked=${lbKey} onChange=${(e) => setLbKey(e.target.checked)} />
      ${" "}Include the lockbox key — the archive can then <b>restore (and decrypt) stored
      secrets</b>; untick for a shareable archive that carries lockbox ciphertext only
    </label>
    <div class="ctl-row"><a class="btn-ghost" href=${"/api/backup" + (qs ? "?" + qs : "")} download>Download backup</a></div>
    <${RestorePanel} />
  `;
}

// Restore an uploaded backup into the server's config home, then the server
// restarts and the rest of the fleet re-pulls + self-populates. The upload rides
// the WS channel (the dashboard HTTP hook takes no body); a realistic archive is
// tens of KB. A force-overwrite of live config, so it's typed-confirm gated.
const _RESTORE_MAX = 8 * 1024 * 1024; // matches the server's WS frame bound

function RestorePanel() {
  const [file, setFile] = useState(null);
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);

  async function doRestore() {
    if (!file) return;
    if (file.size > _RESTORE_MAX) {
      notify("Backup too large for dashboard restore — use kenzy-init --restore.", "err");
      return;
    }
    setBusy(true);
    const buf = new Uint8Array(await file.arrayBuffer());
    let bin = "";
    for (let i = 0; i < buf.length; i++) bin += String.fromCharCode(buf[i]);
    const res = await send("restore", { data: btoa(bin) });
    if (res.ok) {
      notify("Backup restored — server is restarting. The page will reconnect.");
    } else {
      notify(res.error || "Restore failed.", "err");
      setBusy(false);
    }
  }

  return html`
    <details class="restore-box">
      <summary class="micro"><b>Restore from a backup…</b></summary>
      <p class="micro">
        Uploads a backup into this server and <b>overwrites</b> its configuration,
        curation, voice profiles, and any custom skills — then the server restarts and
        the rest of the fleet re-pulls automatically. This replaces live settings, so
        it's a deliberate action. (Custom skills are executable code you authored; an
        upload runs under your admin session. Huge archives with <span class="mono">models/</span>
        use <span class="mono">kenzy-init --restore</span> instead.)
      </p>
      <input type="file" accept=".gz,.tgz,application/gzip"
        disabled=${busy} onChange=${(e) => setFile(e.target.files[0] || null)} />
      <label class="micro" style="display:block; margin-top:.4rem">
        Type <span class="mono">"RESTORE"</span> to confirm:
        <input class="ha-in" style="width:8rem" disabled=${busy}
          value=${confirm} onInput=${(e) => setConfirm(e.target.value)} />
      </label>
      <p><button class="btn-danger" disabled=${busy || !file || confirm !== "RESTORE"}
        onClick=${doRestore}>${busy ? "Restoring…" : "Restore & restart"}</button></p>
    </details>
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
      notify(`${effective} saved — restart the affected services (Fleet → the service) to apply.`);
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
          <button class="btn-primary" disabled=${busy || !effective || !value}>
            ${busy ? "…" : "Set"}
          </button>
        </form>`
      : html`<p class="micro">Read-only — set
          ${" "}<span class="mono">dashboard.controls: true</span> to set keys.</p>`}
  `;
}

// Spoken cues: the pre-recorded phrases Kenzy speaks around a request (the
// failure apology + the processing "Working on it." / "Still working" pools). The
// PHRASES are config (`cues:` in server.yaml); the WAVs are a cache — this
// button re-renders every phrase through the CURRENTLY CONFIGURED TTS voice, so
// after a voice change (or on a local-voice install hearing the bundled cues in
// the wrong voice) one click brings the whole set in line, fleet-wide.
const CUE_LABELS = {
  error: "Failure apology",
  thinking: "Processing status (~5s)",
  working: "Still working (~8s later)",
};

function SpokenCues({ cues, controls }) {
  const [busy, setBusy] = useState(false);
  const texts = (cues && cues.texts) || {};

  async function regenerate() {
    setBusy(true);
    const res = await send("regenerate_cues", {});
    if (!res.ok) {
      notify(res.error || "Could not start cue regeneration.", "err");
      setBusy(false);
      return;
    }
    notify("Rendering spoken cues in the current voice…");
    // The outcome arrives as a cues_result toast; free the button shortly after.
    setTimeout(() => setBusy(false), 4000);
  }

  return html`
    <p class="micro">
      Pre-recorded phrases spoken around a request (they work even when speech
      synthesis is down). Edit the phrases via <span class="mono">cues:</span> in
      server.yaml; regenerate to re-record them all in the current voice.
    </p>
    <dl class="kv">
      ${Object.entries(CUE_LABELS).map(
        ([kind, label]) => html`<dt>${label}</dt>
          <dd>${((texts[kind] || []).length
            ? texts[kind]
            : ["—"]).map((t, i) => html`${i ? html`<br />` : ""}<span class="mono">“${t}”</span>`)}</dd>`,
      )}
    </dl>
    ${controls
      ? html`<div class="ctl-row">
            <button class="btn-ghost" disabled=${busy || !(cues && cues.tts)}
              title=${cues && cues.tts ? "" : "TTS service not configured"}
              onClick=${regenerate}>${busy ? "Rendering…" : "Regenerate spoken cues"}</button>
          </div>
          <p class="micro">Uses the configured TTS voice; applies live to the whole
          fleet — the cues are streamed from the server, so nodes need no update.</p>`
      : html`<p class="micro">Read-only — set
          ${" "}<span class="mono">dashboard.controls: true</span> to regenerate.</p>`}
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
          <${ServerFeatures} controls=${s.flags && s.flags.controls} />
          <dl class="kv">
            ${kv("kenzy version", html`<span class="mono">${s.version}</span>
              ${s.installed && s.installed !== s.version
                ? html` <span class="micro warn">v${s.installed} installed — restart to apply</span>`
                : null}`)}
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
        <header><h2>Spoken cues</h2><span class="rule"></span></header>
        <div class="card pad">
          <${SpokenCues} cues=${s.cues} controls=${s.flags && s.flags.controls} />
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
        <header><h2>Server configuration</h2><span class="rule"></span><a class="docs-link" href="https://docs.kenzy.ai/configuration/server/" target="_blank" rel="noopener">Docs ↗</a></header>
        <div class="card pad"><${ServerSettings} /></div>
      </section>

      <section class="section">
        <header><h2>Controls</h2><span class="rule"></span></header>
        <${ServerControls} controls=${s.flags && s.flags.controls} />
      </section>
    </div>`;
}
