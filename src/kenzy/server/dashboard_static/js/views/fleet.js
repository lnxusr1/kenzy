import { html, useState } from "../html.js";
import { useFleet, send, notify } from "../store.js";

function AnnounceBar() {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  async function go() {
    if (!text.trim()) return;
    setBusy(true);
    const res = await send("announce", { text });
    setBusy(false);
    if (res.ok) {
      notify(`Announcing: “${text.trim()}”`);
      setText("");
    } else {
      notify(res.error || "Announce failed.", "err");
    }
  }
  return html`
    <div class="announce">
      <span class="ico">📢</span>
      <input placeholder="Announce a message to all rooms…" value=${text}
             onInput=${(e) => setText(e.target.value)}
             onKeyDown=${(e) => e.key === "Enter" && go()} />
      <button class="btn-primary" disabled=${busy || !text.trim()} onClick=${go}>
        ${busy ? "Sending…" : "Announce"}</button>
    </div>`;
}

function shortId(id) {
  return id && id.length > 12 ? id.slice(0, 8) + "…" : id;
}

function fmtMetrics(m) {
  if (!m) return null;
  const parts = [];
  if (m.cpu != null) parts.push(`cpu ${m.cpu}%`);
  if (m.ram != null) parts.push(`ram ${m.ram}%`);
  if (m.disk != null) parts.push(`disk ${m.disk}%`);
  if (m.temp != null) parts.push(`${m.temp}°C`);
  return parts.length ? parts.join(" · ") : null;
}

// Elapsed time in the coarsest unit that still reads honestly. Computed in the
// browser from a unix `last_seen` rather than pushed from the server, so an
// absence keeps ageing on screen between state updates — a node that dropped out
// overnight should say "14h", not whatever it said when the last push happened.
function since(lastSeen) {
  if (!lastSeen) return "unknown";
  const secs = Math.max(0, Date.now() / 1000 - lastSeen);
  if (secs < 90) return `${Math.round(secs)}s`;
  if (secs < 5400) return `${Math.round(secs / 60)}m`;
  if (secs < 172800) return `${Math.round(secs / 3600)}h`;
  return `${Math.round(secs / 86400)}d`;
}

// An absent node is a fault once it has been gone longer than the configured
// threshold and is not inside an expected-downtime window (we restarted it).
export function isAlerting(node, alertSeconds) {
  if (node.connected || !alertSeconds) return false;
  const now = Date.now() / 1000;
  if (node.grace_until && now < node.grace_until) return false;
  return now - (node.last_seen || 0) >= alertSeconds;
}

function NodeCard({ node, onConfigure, onForget, alerting }) {
  const offline = !node.connected;
  const streaming = node.streaming;
  const audioFailed = node.audio_ok === false;
  const title = node.room || node.node_id;
  const led = offline ? "down" : audioFailed ? "down" : streaming ? "busy" : "up";
  const status = offline
    ? `offline ${since(node.last_seen)}`
    : audioFailed
      ? "audio error"
      : streaming
        ? "streaming"
        : "idle";
  return html`
    <div class=${"card" + (offline ? " node-offline" : "") + (alerting ? " node-alert" : "")}>
      <div class="top">
        <span class=${"led " + led}></span>
        <span class="room">${title}</span>
        <span class=${"badge" + (streaming ? " streaming" : "") + (alerting ? " warn" : "")}>
          ${status}</span>
      </div>
      <dl>
        <dt>node</dt><dd title=${node.node_id}>${shortId(node.node_id)}</dd>
        <dt>address</dt><dd>${node.ip || "—"}</dd>
        ${offline
          ? html`<dt>last seen</dt><dd title=${node.last_seen ? new Date(node.last_seen * 1000).toLocaleString() : ""}>${since(node.last_seen)} ago</dd>`
          : html`<dt>session</dt><dd>${node.session_id || "—"}</dd>`}
        <dt>link</dt><dd>${node.connected ? "connected" : "down"}</dd>
        <dt>version</dt><dd>${node.version || "—"}</dd>
        ${fmtMetrics(node.metrics)
          ? html`<dt>system</dt><dd class=${node.metrics.temp != null && node.metrics.temp >= 80 ? "hot" : ""}>${fmtMetrics(node.metrics)}</dd>`
          : null}
      </dl>
      ${alerting
        ? html`<div class="unclaimed" title="This room has been unreachable long enough to be a fault. Check the node host: power, network, and its clock (a join is refused if the node's time is more than two minutes off the server's).">⚠ unreachable for ${since(node.last_seen)} — check the node host</div>`
        : null}
      ${node.aec === false
        ? html`<div class="unclaimed" title="hardware_aec: false — no echo cancellation: voice interrupt during playback, intercom, and alarm ring loops are disabled for this room">◌ no AEC — half-duplex room</div>`
        : null}
      ${audioFailed
        ? html`<div class="unclaimed" title=${node.audio_error || ""}>⚠ audio failed — check the device, then Restart</div>`
        : null}
      ${!node.configured ? html`<div class="unclaimed">⚑ unconfigured</div>` : null}
      <button class="btn-ghost card-cfg" onClick=${() => onConfigure(node.node_id)}>Configure</button>
      ${offline && onForget
        ? html`<button class="btn-ghost card-cfg" title="Remove this node from the fleet roster. Use when it has been decommissioned — otherwise it stays listed as missing." onClick=${() => onForget(node)}>Forget</button>`
        : null}
    </div>
  `;
}

function ServiceChip({ svc, onConfigure }) {
  const detail = svc.detail || {};
  // `version` is the RUNNING code; `installed` is what's on disk. They differ
  // when the shared venv was upgraded but this service wasn't recycled yet.
  const stale = detail.installed && detail.version && detail.installed !== detail.version;
  const bits = [detail.version && "v" + detail.version, detail.model, detail.provider, detail.voice]
    .filter(Boolean)
    .join(" · ");
  return html`
    <div class="chip" role="button" tabindex="0"
         title=${`Configure ${svc.name}${svc.url ? " — " + svc.url : ""}`}
         onClick=${() => onConfigure(svc.name)}>
      <span class=${"led " + (svc.up ? "up" : "down")}></span>
      <span class="name">${svc.name}</span>
      <span class="detail">${svc.up ? bits || "ok" : "down"}</span>
      ${stale
        ? html`<span class="badge warn" title=${`v${detail.installed} installed — restart to apply`}>restart</span>`
        : null}
    </div>
  `;
}

export function FleetView({ onConfigure, onConfigureService }) {
  const { data, loading, error } = useFleet();
  if (loading && !data) return html`<div class="empty">Loading fleet…</div>`;
  if (error && !data) return html`<div class="empty">Could not reach the server: ${error}</div>`;

  const nodes = (data && data.nodes) || [];
  const services = (data && data.services) || [];
  const upCount = services.filter((s) => s.up).length;
  const flags = (data && data.flags) || {};
  const online = nodes.filter((n) => n.connected);
  const offline = nodes.filter((n) => !n.connected);
  const alerts = offline.filter((n) => isAlerting(n, flags.offline_alert_s));

  async function forget(node) {
    const name = node.room || node.node_id;
    const res = await send("forget_node", { node: node.node_id });
    notify(res.ok ? `Forgot ${name}.` : res.error || "Could not forget that node.",
           res.ok ? "ok" : "err");
  }

  return html`
    <div class="stats">
      <div class="tile"><div class="micro">Nodes online</div>
        <div class="k">${online.length}${offline.length
          ? html`<small>/${nodes.length}</small>`
          : null}</div></div>
      <div class="tile"><div class="micro">Services up</div>
        <div class="k">${upCount}<small>/${services.length || 0}</small></div></div>
      <div class="tile"><div class="micro">Streaming now</div>
        <div class="k">${nodes.filter((n) => n.streaming).length}</div></div>
    </div>

    ${alerts.length
      ? html`<div class="banner warn" role="alert">⚠ ${alerts.length === 1
          ? html`<b>${alerts[0].room || alerts[0].node_id}</b> has been offline for ${since(alerts[0].last_seen)}`
          : html`<b>${alerts.length} rooms</b> are offline: ${alerts.map((n) => n.room || n.node_id).join(", ")}`}${" "}
          — a room that cannot reach the server still answers its wake word, so it will look fine from inside it.</div>`
      : null}

    ${data && data.flags && data.flags.controls && online.length ? html`<${AnnounceBar} />` : null}

    <section class="section">
      <header><h2>Room nodes</h2><span class="rule"></span></header>
      ${nodes.length
        ? html`<div class="grid">${nodes.map(
            (n) => html`<${NodeCard} key=${n.node_id} node=${n} onConfigure=${onConfigure}
                          alerting=${isAlerting(n, flags.offline_alert_s)}
                          onForget=${flags.controls ? forget : null} />`,
          )}</div>`
        : html`<div class="empty">No nodes connected yet.</div>`}
    </section>

    <section class="section">
      <header><h2>Backend services</h2><span class="rule"></span></header>
      ${services.length
        ? html`<div class="chips">${services.map(
            (s) => html`<${ServiceChip} key=${s.name} svc=${s} onConfigure=${onConfigureService} />`,
          )}</div>`
        : html`<div class="empty">No backend services configured.</div>`}
    </section>
  `;
}
