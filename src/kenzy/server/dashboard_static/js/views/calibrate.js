// Shared calibration primitives used by the audio-setup wizard: meter scaling,
// percentile stats, threshold suggestions, the <Meter> bar, and a live tune-stream
// hook. (Mirrors the Python suggestion heuristics in kenzy/node/client.py.)
import { html, useState, useEffect, useRef } from "../html.js";
import { send, subscribeTune } from "../store.js";

// RMS (0..32767) is log-scaled so the quiet end has resolution; scores are 0..1.
export const logPct = (v) => (v <= 0 ? 0 : Math.min(100, (Math.log10(v + 1) / Math.log10(32768)) * 100));
export const linPct = (v) => Math.max(0, Math.min(100, v * 100));
export const round2 = (v) => Math.round(v * 100) / 100;

export function pstats(arr) {
  if (!arr.length) return null;
  const s = arr.slice().sort((a, b) => a - b);
  const p = (q) => s[Math.min(s.length - 1, Math.max(0, Math.floor(q * (s.length - 1))))];
  return { n: s.length, min: p(0), p50: p(0.5), p75: p(0.75), p90: p(0.9), max: p(1) };
}

export function rmsSuggest(st) {
  if (!st) return null;
  return Math.max(5, Math.min(5000, Math.round(Math.max(st.p90 * 1.5, st.p90 + 15))));
}
export function wakeSuggest(st) {
  if (!st) return null;
  const gap = st.max - st.p75;
  if (gap < 0.15) return null;
  return round2(Math.max(0.05, Math.min(0.95, st.p75 + gap * 0.4)));
}
export function vadSuggest(st) {
  if (!st) return null;
  const gap = st.max - st.p75;
  if (gap < 0.15) return null;
  return round2(Math.max(0.0, Math.min(0.9, st.p75 + gap * 0.3)));
}

export function Meter({ pct, marks = [] }) {
  return html`
    <div class="meter">
      <div class="meter-fill" style=${`width:${pct}%`}></div>
      ${marks.map((m) =>
        m.at == null
          ? null
          : html`<div class=${"meter-mark " + m.cls} style=${`left:${m.pos}%`} title=${m.title}></div>`,
      )}
    </div>`;
}

/**
 * Live tune-stream hook: drives tune_start/tune_stop for one node and accumulates
 * per-frame rms/wake/vad into rolling stats. Auto-stops the node window on unmount.
 * Returns { running, latest, stats, start, stop }.
 */
export function useTuneStream(node) {
  const [running, setRunning] = useState(false);
  const [latest, setLatest] = useState({ rms: 0, wake: 0, vad: 0 });
  const [stats, setStats] = useState({ rms: null, wake: null, vad: null });
  const buf = useRef({ rms: [], wake: [], vad: [] });
  const runningRef = useRef(false);

  useEffect(() => {
    runningRef.current = running;
  }, [running]);

  useEffect(() => {
    if (!running) return undefined;
    return subscribeTune((m) => {
      if (m.node !== node) return;
      const s = m.sample || {};
      if (s.stopped) {
        setRunning(false);
        return;
      }
      const rms = Number(s.rms) || 0;
      const wake = Number(s.wake) || 0;
      const vad = Number(s.vad) || 0;
      const b = buf.current;
      b.rms.push(rms);
      b.wake.push(wake);
      b.vad.push(vad);
      for (const k of ["rms", "wake", "vad"]) if (b[k].length > 600) b[k].shift();
      setLatest({ rms, wake, vad });
      setStats({ rms: pstats(b.rms), wake: pstats(b.wake), vad: pstats(b.vad) });
    });
  }, [running, node]);

  useEffect(
    () => () => {
      if (runningRef.current) send("tune_stop", { node });
    },
    [node],
  );

  async function start() {
    buf.current = { rms: [], wake: [], vad: [] };
    setStats({ rms: null, wake: null, vad: null });
    setLatest({ rms: 0, wake: 0, vad: 0 });
    const res = await send("tune_start", { node, seconds: 30 });
    if (res.ok) setRunning(true);
    return res;
  }

  async function stop() {
    setRunning(false);
    await send("tune_stop", { node });
  }

  return { running, latest, stats, start, stop };
}
