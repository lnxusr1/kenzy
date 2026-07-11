// Calibration rendering primitives for the audio-setup wizard: meter scaling and
// the <Meter> bar. All calibration MATH lives server-side in kenzy/calibration.py
// (single source for the guided session and the `kenzy-node --calibrate` CLI) —
// the browser only renders live samples and progress events.
import { html } from "../html.js";

// RMS (0..32767) is log-scaled so the quiet end has resolution; scores are 0..1.
export const logPct = (v) => (v <= 0 ? 0 : Math.min(100, (Math.log10(v + 1) / Math.log10(32768)) * 100));
export const linPct = (v) => Math.max(0, Math.min(100, v * 100));
export const round2 = (v) => Math.round(v * 100) / 100;

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
