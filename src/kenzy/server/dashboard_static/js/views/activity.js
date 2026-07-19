import { html, useState, useEffect } from "../html.js";
import { subscribeSession } from "../store.js";

const ms = (v) => (v >= 1000 ? (v / 1000).toFixed(1) + "s" : Math.round(v || 0) + "ms");
const clock = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString() : "");

// Latency waterfall: capture (STT+speaker in parallel → max), LLM, TTS. Widths
// map to absolute milliseconds against a SHARED scale (scaleMs = full track
// width), so a segment's width means the same real duration on every row —
// a slow LLM run is visibly wider than a fast one, and fast-path runs read as
// slivers. The gap to the row's own total is round-trip/overhead (left blank).
function timeline(s, scaleMs) {
  const cap = Math.max(s.stt_ms || 0, s.speaker_ms || 0);
  const seg = (v, cls, label) =>
    v > 0
      ? html`<span class=${"tg " + cls} style=${`width:${Math.min(100, (v / scaleMs) * 100).toFixed(1)}%`}
          title=${`${label}: ${Math.round(v)}ms`}></span>`
      : null;
  // With per-span detail from the LLM service, the LLM segment is subdivided:
  // model calls vs tool calls (in order), the remainder rendered as the plain
  // LLM shade (service overhead). Same shared scale, so sub-widths are real.
  const spans = Array.isArray(s.spans) ? s.spans.filter((x) => x && x.ms > 0) : [];
  const spanSum = spans.reduce((a, x) => a + x.ms, 0);
  const llmSegs = spans.length
    ? html`${spans.map((x) =>
        seg(
          x.ms,
          x.kind === "model" ? "llm-model" : x.kind === "tool" ? "llm-tool" : "llm",
          `${x.kind}${x.name ? " " + x.name : ""}`,
        ),
      )}${seg(Math.max(0, (s.llm_ms || 0) - spanSum), "llm", "LLM overhead")}`
    : seg(s.llm_ms, "llm", "LLM");
  return html`<div class="timeline">
    ${seg(cap, "cap", "capture (STT + speaker)")}${llmSegs}${seg(s.tts_ms, "tts", "TTS")}
  </div>`;
}

// The expanded per-row breakdown: ordered spans with names, plus overhead.
function spanDetail(s) {
  const spans = Array.isArray(s.spans) ? s.spans : [];
  if (!spans.length) return null;
  const spanSum = spans.reduce((a, x) => a + (x.ms || 0), 0);
  const over = Math.max(0, (s.llm_ms || 0) - spanSum);
  const icon = { fast: "⚡", model: "◆", tool: "⚒" };
  return html`<div class="sess-spans micro">
    ${spans.map(
      (x, i) => html`<span class="sess-span" key=${i}>
        ${icon[x.kind] || "·"} ${x.name || x.kind} <b>${ms(x.ms)}</b>
      </span>`,
    )}
    ${over > 0 && spans[0] && spans[0].kind !== "fast"
      ? html`<span class="sess-span">· overhead <b>${ms(over)}</b></span>`
      : null}
  </div>`;
}

// Round a max up to a tidy axis ceiling so the shared scale has a clean label.
function niceCeil(v) {
  if (v <= 1000) return 1000;
  if (v <= 2000) return Math.ceil(v / 250) * 250;
  if (v <= 5000) return Math.ceil(v / 500) * 500;
  return Math.ceil(v / 1000) * 1000;
}

export function ActivityView() {
  const [sessions, setSessions] = useState(null);
  const [open, setOpen] = useState(null); // expanded row index (span breakdown)

  useEffect(() => {
    fetch("/api/sessions")
      .then((r) => r.json())
      .then((d) => setSessions(d.sessions || []))
      .catch(() => setSessions([]));
    return subscribeSession((rec) => setSessions((cur) => [rec, ...(cur || [])].slice(0, 200)));
  }, []);

  if (sessions === null) return html`<div class="empty">Loading…</div>`;
  if (!sessions.length)
    return html`<div class="empty">No activity recorded yet. Requires
      ${" "}<span class="mono">dashboard.logs: true</span>; talk to a node to populate it.</div>`;

  const n = sessions.length;
  const fastN = sessions.filter((s) => s.fast).length;
  const avg = sessions.reduce((a, s) => a + (s.total_ms || 0), 0) / n;
  // Shared time axis across every visible run (min 1s so fast-path slivers
  // aren't blown up). Full track width == scaleMs, so bars are comparable.
  const scaleMs = niceCeil(Math.max(1000, ...sessions.map((s) => s.total_ms || 0)));

  return html`
    <div class="stats">
      <div class="tile"><div class="micro">Recent interactions</div><div class="k">${n}</div></div>
      <div class="tile"><div class="micro">Fast-path hit rate</div>
        <div class="k">${Math.round((fastN / n) * 100)}<small>%</small></div></div>
      <div class="tile"><div class="micro">Avg response time</div><div class="k">${ms(avg)}</div></div>
    </div>

    <section class="section">
      <header><h2>Recent activity</h2><span class="rule"></span></header>
      <div class="leg">
        <span><span class="tg cap"></span> capture</span>
        <span><span class="tg llm-model"></span> model</span>
        <span><span class="tg llm-tool"></span> tools</span>
        <span><span class="tg llm"></span> LLM other</span>
        <span><span class="tg tts"></span> TTS</span>
        <span class="leg-scale">to scale · full width = ${ms(scaleMs)}</span>
      </div>
      <div class="sessions">
        ${sessions.map(
          (s, i) => html`
            <div class=${"sess" + (s.spans && s.spans.length ? " has-spans" : "")} key=${i}
              onClick=${() => setOpen(open === i ? null : i)}>
              <div class="sess-head">
                <span class=${"badge" + (s.fast ? " fast" : "")}>${s.fast ? "fast" : "LLM"}</span>
                <span class="sess-room mono">${s.room}</span>
                ${s.speaker && s.speaker !== "unknown"
                  ? html`<span class="micro">${s.speaker}</span>`
                  : null}
                <span class="spacer"></span>
                <span class="micro">${clock(s.ts)}</span>
                <span class="sess-total mono">${ms(s.total_ms)}</span>
              </div>
              <div class="sess-line you">“${s.transcript}”</div>
              <div class="sess-line reply">${s.response}</div>
              ${timeline(s, scaleMs)}
              ${open === i ? spanDetail(s) : null}
            </div>
          `,
        )}
      </div>
    </section>`;
}
