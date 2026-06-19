import { html, useState, useEffect } from "../html.js";
import { useFleet } from "../store.js";

const LEVELS = ["", "DEBUG", "INFO", "WARNING", "ERROR"];

function fmtTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleTimeString() : "";
}

export function LogsView() {
  const { data } = useFleet();
  const services = ((data && data.services) || []).map((s) => s.name);
  const nodes = (data && data.nodes) || [];
  const sources = [
    { id: "server", label: "Server", url: "/api/logs" },
    ...services.map((n) => ({ id: `svc:${n}`, label: `service · ${n}`, url: `/api/services/${n}/logs` })),
    ...nodes.map((n) => ({
      id: `node:${n.node_id}`,
      label: `node · ${n.room || n.node_id}`,
      url: `/api/nodes/${encodeURIComponent(n.node_id)}/logs`,
    })),
  ];

  const [src, setSrc] = useState("server");
  const [level, setLevel] = useState("");
  const [lines, setLines] = useState([]);
  const [note, setNote] = useState("");
  const [loading, setLoading] = useState(false);

  const current = sources.find((s) => s.id === src) || sources[0];

  async function load() {
    setLoading(true);
    setNote("");
    try {
      const r = await fetch(`${current.url}?level=${encodeURIComponent(level)}&limit=300`);
      const d = await r.json();
      const logs = d.logs || [];
      setLines(logs);
      if (current.id.startsWith("node:") && d.reachable === false)
        setNote("Node not reachable (offline, or logs not enabled on it yet).");
      else if (!logs.length) setNote("No log lines in the buffer.");
    } catch {
      setLines([]);
      setNote("Could not load logs.");
    }
    setLoading(false);
  }

  useEffect(() => {
    load();
  }, [src, level]);

  return html`
    <div class="logs">
      <div class="logs-bar">
        <select value=${src} onChange=${(e) => setSrc(e.target.value)}>
          ${sources.map((s) => html`<option value=${s.id} selected=${s.id === src}>${s.label}</option>`)}
        </select>
        <select value=${level} onChange=${(e) => setLevel(e.target.value)}>
          ${LEVELS.map((l) => html`<option value=${l} selected=${l === level}>${l || "all levels"}</option>`)}
        </select>
        <button class="btn-ghost" onClick=${load}>${loading ? "…" : "Refresh"}</button>
      </div>
      ${note ? html`<div class="empty">${note}</div>` : null}
      ${lines.length
        ? html`<div class="logview">
            ${lines.map(
              (e, i) => html`
                <div class=${"logline lv-" + (e.level || "").toLowerCase()} key=${i}>
                  <span class="lt">${fmtTime(e.ts)}</span>
                  <span class="ll">${e.level}</span>
                  <span class="ln">${e.name}</span>
                  <span class="lm">${e.msg}</span>
                </div>
              `,
            )}
          </div>`
        : null}
    </div>`;
}
