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

function NodeCard({ node, onConfigure }) {
  const streaming = node.streaming;
  const title = node.display_name || node.room_id;
  return html`
    <div class="card">
      <div class="top">
        <span class=${"led " + (streaming ? "busy" : "up")}></span>
        <span class="room">${title}</span>
        <span class=${"badge" + (streaming ? " streaming" : "")}>${streaming ? "streaming" : "idle"}</span>
      </div>
      <dl>
        ${node.display_name ? html`<dt>room id</dt><dd>${node.room_id}</dd>` : null}
        <dt>address</dt><dd>${node.ip || "—"}</dd>
        <dt>session</dt><dd>${node.session_id || "—"}</dd>
        <dt>link</dt><dd>${node.connected ? "connected" : "down"}</dd>
      </dl>
      ${!node.configured ? html`<div class="unclaimed">⚑ unconfigured</div>` : null}
      <button class="btn-ghost card-cfg" onClick=${() => onConfigure(node.room_id)}>Configure</button>
    </div>
  `;
}

function ServiceChip({ svc }) {
  const detail = svc.detail || {};
  const bits = [detail.model, detail.provider, detail.voice].filter(Boolean).join(" · ");
  return html`
    <div class="chip">
      <span class=${"led " + (svc.up ? "up" : "down")}></span>
      <span class="name">${svc.name}</span>
      <span class="detail">${svc.up ? bits || "ok" : "down"}</span>
    </div>
  `;
}

export function FleetView({ onConfigure }) {
  const { data, loading, error } = useFleet();
  if (loading && !data) return html`<div class="empty">Loading fleet…</div>`;
  if (error && !data) return html`<div class="empty">Could not reach the server: ${error}</div>`;

  const nodes = (data && data.nodes) || [];
  const services = (data && data.services) || [];
  const upCount = services.filter((s) => s.up).length;

  return html`
    <div class="stats">
      <div class="tile"><div class="micro">Nodes online</div>
        <div class="k">${nodes.length}</div></div>
      <div class="tile"><div class="micro">Services up</div>
        <div class="k">${upCount}<small>/${services.length || 0}</small></div></div>
      <div class="tile"><div class="micro">Streaming now</div>
        <div class="k">${nodes.filter((n) => n.streaming).length}</div></div>
    </div>

    ${data && data.flags && data.flags.controls && nodes.length ? html`<${AnnounceBar} />` : null}

    <section class="section">
      <header><h2>Room nodes</h2><span class="rule"></span></header>
      ${nodes.length
        ? html`<div class="grid">${nodes.map(
            (n) => html`<${NodeCard} key=${n.room_id} node=${n} onConfigure=${onConfigure} />`,
          )}</div>`
        : html`<div class="empty">No nodes connected yet.</div>`}
    </section>

    <section class="section">
      <header><h2>Backend services</h2><span class="rule"></span></header>
      ${services.length
        ? html`<div class="chips">${services.map((s) => html`<${ServiceChip} key=${s.name} svc=${s} />`)}</div>`
        : html`<div class="empty">No backend services configured.</div>`}
    </section>
  `;
}
