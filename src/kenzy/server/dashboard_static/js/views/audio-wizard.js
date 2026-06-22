// Optional, launch-on-demand modal that guides audio setup for one node:
// Overview → Device → Silence → Wake word → Finish. Reuses the shared calibration
// primitives; no server/protocol additions (set_override + restart + tune_*).
import { html, useState, useEffect, useRef } from "../html.js";
import { send, notify, useFleet } from "../store.js";
import {
  Meter,
  useTuneStream,
  logPct,
  linPct,
  round2,
  rmsSuggest,
  wakeSuggest,
  vadSuggest,
} from "./calibrate.js";

const fmt = (v) => (v === undefined || v === null || v === "" ? "default" : v);

export function AudioWizard({ node, info, onClose, onApplied }) {
  const [step, setStep] = useState("overview");
  const [applied, setApplied] = useState({});
  const appliedRef = useRef({});
  const [pendingVad, setPendingVad] = useState(null);
  const [devSel, setDevSel] = useState("");
  const [devPhase, setDevPhase] = useState("idle"); // idle|saving|restarting|timeout
  const sawDrop = useRef(false);
  const tune = useTuneStream(node);
  const fleet = useFleet();

  const devices = (info.devices || []).filter((d) => d.suggested);
  const cur = (k) => (applied[k] !== undefined ? applied[k] : info.config[k]);

  // Stop any running measurement when leaving a measuring step.
  useEffect(() => {
    if (tune.running) tune.stop();
  }, [step]);

  // After a device restart, advance once the node has dropped and come back.
  useEffect(() => {
    if (devPhase !== "restarting") return undefined;
    const n = (fleet.data?.nodes || []).find((x) => x.node_id === node);
    const connected = !!(n && n.connected);
    if (!connected) sawDrop.current = true;
    else if (sawDrop.current) {
      setDevPhase("idle");
      setStep("silence");
    }
    return undefined;
  }, [fleet, devPhase]);

  useEffect(() => {
    if (devPhase !== "restarting") return undefined;
    const t = setTimeout(() => setDevPhase("timeout"), 35000);
    return () => clearTimeout(t);
  }, [devPhase]);

  async function applyPatch(patch) {
    const base = { ...(info.override || {}) };
    delete base.room_id; // server-managed; rejected by set_override
    const config = { ...base, ...appliedRef.current, ...patch };
    const res = await send("set_override", { node, config });
    if (res.ok) {
      appliedRef.current = { ...appliedRef.current, ...patch };
      setApplied(appliedRef.current);
      if (onApplied) onApplied();
    } else {
      notify(res.error || "Could not save.", "err");
    }
    return res;
  }

  async function applyDevice() {
    const d = devices.find((x) => String(x.index) === String(devSel));
    if (!d) return;
    setDevPhase("saving");
    const res = await applyPatch({
      audio_device: d.suggested.audio_device,
      capture_sample_rate: d.suggested.capture_sample_rate,
      playback_sample_rate: d.suggested.playback_sample_rate,
    });
    if (!res.ok) {
      setDevPhase("idle");
      return;
    }
    await send("restart", { node });
    sawDrop.current = false;
    setDevPhase("restarting");
  }

  async function finish() {
    if (pendingVad != null) {
      const res = await applyPatch({ wakeword_vad_threshold: pendingVad });
      if (res.ok) {
        await send("restart", { node });
        notify("VAD gate applied — node restarting.");
      }
    }
    onClose();
  }

  // ---- step bodies --------------------------------------------------------

  function overview() {
    const n = (fleet.data?.nodes || []).find((x) => x.node_id === node);
    const audioBad = n && n.audio_ok === false;
    return html`
      ${audioBad
        ? html`<div class="banner">⚠ This node reports an audio problem — start with the device step.</div>`
        : null}
      <dl class="wiz-cur">
        <dt>device</dt><dd class="mono">${fmt(cur("audio_device"))}</dd>
        <dt>silence threshold</dt><dd class="mono">${fmt(cur("silence_rms_threshold"))}</dd>
        <dt>wake threshold</dt><dd class="mono">${fmt(cur("wakeword_threshold"))}</dd>
        <dt>VAD gate</dt><dd class="mono">${fmt(cur("wakeword_vad_threshold"))}</dd>
      </dl>
      <p class="micro">Run the full setup, or jump to one step to recalibrate it.</p>
      <div class="wiz-actions">
        <button class="btn-primary" onClick=${() => setStep("device")}>Start full setup</button>
        <button class="btn-ghost" onClick=${() => setStep("device")}>Device</button>
        <button class="btn-ghost" onClick=${() => setStep("silence")}>Silence</button>
        <button class="btn-ghost" onClick=${() => setStep("wake")}>Wake word</button>
      </div>`;
  }

  function deviceStep() {
    if (devPhase === "restarting") {
      return html`<p>Restarting <strong>${info.room || node}</strong> to apply the device…
        <span class="micro">waiting for it to reconnect.</span></p>
        <div class="wiz-actions"><span class="spinner"></span></div>`;
    }
    if (devPhase === "timeout") {
      return html`<p>The node is taking a while to come back.</p>
        <div class="wiz-actions">
          <button class="btn-ghost" onClick=${() => setStep("silence")}>Continue anyway</button>
          <button class="btn-ghost" onClick=${onClose}>Close</button>
        </div>`;
    }
    return html`
      <p class="micro">Pick the room's mic/speaker. Changing it restarts the node so the
        next steps measure the right device.</p>
      ${devices.length
        ? html`<select class="audio-select" disabled=${devPhase === "saving"}
            value=${devSel} onChange=${(e) => setDevSel(e.target.value)}>
            <option value="">choose a device…</option>
            ${devices.map((d) => html`<option value=${d.index}>${d.name}</option>`)}
          </select>`
        : html`<p class="micro">No selectable devices reported by this node.</p>`}
      <p class="audio-current">Current: <span class="mono">${fmt(cur("audio_device"))}</span></p>
      <p class="wiz-hint">${devSel
        ? "Click Apply & restart to switch to this device."
        : "Choose a device above, or keep the current one to continue."}</p>
      ${wizFooter(
        html`<button class=${devSel ? "btn-primary" : "btn-ghost"} disabled=${!devSel || devPhase === "saving"}
          onClick=${applyDevice}>${devPhase === "saving" ? "Saving…" : "Apply & restart"}</button>
        <button class=${devSel ? "btn-ghost" : "btn-primary"}
          onClick=${() => setStep("silence")}>Keep current →</button>`,
      )}`;
  }

  function silenceStep() {
    const sug = rmsSuggest(tune.stats.rms);
    const cv = cur("silence_rms_threshold");
    const done = applied.silence_rms_threshold !== undefined;
    // The single "next action" that gets the primary highlight, in order.
    const stage = done
      ? "next"
      : sug != null
        ? "apply"
        : tune.running
          ? "measure"
          : "start";
    const hint = {
      start: "1. Click Start, then stay quiet for a few seconds.",
      measure: "Listening… keep the room quiet.",
      apply: "2. Looks good — click Apply to set the threshold.",
      next: "✓ Applied. Click Next to continue.",
    }[stage];
    return html`
      <p class="micro">Stay quiet so it measures the room's noise floor. Applies live.</p>
      <${Meter} pct=${logPct(tune.latest.rms)} marks=${[
        { at: cv, pos: logPct(cv || 0), cls: "cur", title: `current ${cv}` },
        { at: sug, pos: logPct(sug || 0), cls: "sug", title: `suggested ${sug}` },
      ]} />
      <p class="calib-read">RMS <span class="mono">${Math.round(tune.latest.rms)}</span>
        ${tune.stats.rms
          ? html` · floor <span class="mono">${tune.stats.rms.p90}</span> · suggest
              <span class="mono">${sug ?? "—"}</span> · current <span class="mono">${fmt(cv)}</span>`
          : html` · ${tune.running ? "listening…" : "not started"}`}</p>
      <p class="wiz-hint">${hint}</p>
      <div class="wiz-actions">
        ${tune.running
          ? html`<button class="btn-ghost" onClick=${tune.stop}>Stop</button>`
          : html`<button class=${stage === "start" ? "btn-primary" : "btn-ghost"}
              onClick=${tune.start}>${done ? "Re-measure" : "Start"}</button>`}
        <button class=${stage === "apply" ? "btn-primary" : "btn-ghost"} disabled=${sug == null}
          onClick=${() => applyPatch({ silence_rms_threshold: sug })}>
          ${done ? "✓ Applied" : `Apply${sug != null ? ` ${sug}` : ""}`}</button>
      </div>
      ${wizFooter(html`<button class=${stage === "next" ? "btn-primary" : "btn-ghost"}
        onClick=${() => setStep("wake")}>Next: wake word →</button>`)}`;
  }

  function wakeStep() {
    const sw = wakeSuggest(tune.stats.wake);
    const sv = vadSuggest(tune.stats.vad);
    const cw = cur("wakeword_threshold");
    const cvd = cur("wakeword_vad_threshold");
    const wakeDone = applied.wakeword_threshold !== undefined;
    const vadQueued = pendingVad != null;
    // The single "next action" that gets the primary highlight, in order:
    // start → measure → apply wake → queue VAD → next.
    let stage;
    if (!wakeDone) stage = sw != null ? "applyWake" : tune.running ? "measure" : "start";
    else if (sv != null && !vadQueued) stage = "queueVad";
    else stage = "next";
    const hint = {
      start: "1. Click Start, then say “Hey Kenzy” a few times.",
      measure: "Say “Hey Kenzy” a few times…",
      applyWake: "2. Click Apply wake to set the wake-word threshold.",
      queueVad: "3. Click Queue VAD to reduce false triggers (applied on Finish).",
      next: "✓ Done — click Next to finish.",
    }[stage];
    return html`
      <p class="micro">Say the wake word ("Hey Kenzy") a few times.</p>
      <div class="calib-grp"><span class="micro">wake score</span>
        <${Meter} pct=${linPct(tune.latest.wake)} marks=${[
          { at: cw, pos: linPct(cw || 0), cls: "cur", title: `current ${cw}` },
          { at: sw, pos: linPct(sw || 0), cls: "sug", title: `suggested ${sw}` },
        ]} />
        <p class="calib-read">score <span class="mono">${round2(tune.latest.wake)}</span>
          ${tune.stats.wake
            ? html` · peak <span class="mono">${round2(tune.stats.wake.max)}</span> · suggest
                <span class="mono">${sw ?? "—"}</span> · current <span class="mono">${fmt(cw)}</span>`
            : html` · ${tune.running ? "listening…" : "not started"}`}</p>
      </div>
      <div class="calib-grp"><span class="micro">voice-activity (VAD) score</span>
        <${Meter} pct=${linPct(tune.latest.vad)} marks=${[
          { at: cvd, pos: linPct(cvd || 0), cls: "cur", title: `current ${cvd}` },
          { at: pendingVad ?? sv, pos: linPct((pendingVad ?? sv) || 0), cls: "sug", title: `suggested ${sv}` },
        ]} />
        <p class="calib-read">score <span class="mono">${round2(tune.latest.vad)}</span>
          ${tune.stats.vad
            ? html` · suggest <span class="mono">${sv ?? "—"}</span> · current
                <span class="mono">${fmt(cvd)}</span>
                ${pendingVad != null ? html` · queued <span class="mono">${pendingVad}</span>` : null}`
            : null}</p>
      </div>
      <p class="wiz-hint">${hint}</p>
      <div class="wiz-actions">
        ${tune.running
          ? html`<button class="btn-ghost" onClick=${tune.stop}>Stop</button>`
          : html`<button class=${stage === "start" ? "btn-primary" : "btn-ghost"}
              onClick=${tune.start}>${wakeDone ? "Re-measure" : "Start"}</button>`}
        <button class=${stage === "applyWake" ? "btn-primary" : "btn-ghost"} disabled=${sw == null}
          onClick=${() => applyPatch({ wakeword_threshold: sw })}>
          ${wakeDone ? "✓ Wake set" : `Apply wake${sw != null ? ` ${sw}` : ""}`}</button>
        <button class=${stage === "queueVad" ? "btn-primary" : "btn-ghost"}
          disabled=${!wakeDone || sv == null}
          onClick=${() => setPendingVad(sv)}>
          ${vadQueued ? "✓ VAD queued" : `Queue VAD${sv != null ? ` ${sv}` : ""} (restart)`}</button>
      </div>
      ${wizFooter(html`<button class=${stage === "next" ? "btn-primary" : "btn-ghost"}
        onClick=${() => setStep("finish")}>Next: finish →</button>`)}`;
  }

  function finishStep() {
    const keys = Object.keys(applied);
    return html`
      <p>Setup complete.</p>
      ${keys.length
        ? html`<dl class="wiz-cur">${keys.map(
            (k) => html`<dt class="mono">${k}</dt><dd class="mono">${applied[k]}</dd>`,
          )}</dl>`
        : html`<p class="micro">No changes were applied.</p>`}
      ${pendingVad != null
        ? html`<p class="micro">VAD gate <span class="mono">${pendingVad}</span> will be applied
            and the node restarted on Finish.</p>`
        : null}
      <div class="wiz-actions">
        <button class="btn-primary" onClick=${finish}>Finish${pendingVad != null ? " & restart" : ""}</button>
      </div>`;
  }

  function wizFooter(actions) {
    return html`<div class="wiz-footer">
      <button class="btn-ghost" onClick=${() => setStep("overview")}>← Steps</button>
      <div class="wiz-footer-actions">${actions}</div>
    </div>`;
  }

  let body;
  if (!info.connected) body = html`<p>Node must be connected to set up audio.</p>`;
  else if (!info.controls)
    body = html`<p>Enable <code class="mono">dashboard.controls</code> to use the wizard.</p>`;
  else if (step === "device") body = deviceStep();
  else if (step === "silence") body = silenceStep();
  else if (step === "wake") body = wakeStep();
  else if (step === "finish") body = finishStep();
  else body = overview();

  const titles = {
    overview: "Audio setup",
    device: "Step 1 — Audio device",
    silence: "Step 2 — Silence threshold",
    wake: "Step 3 — Wake word",
    finish: "Finish",
  };

  return html`
    <div class="modal-backdrop" onClick=${onClose}>
      <div class="modal" onClick=${(e) => e.stopPropagation()}>
        <header class="modal-head">
          <h3>${titles[step]} — ${info.room || node}</h3>
          <button class="modal-x" title="Close" onClick=${onClose}>×</button>
        </header>
        <div class="modal-body">${body}</div>
      </div>
    </div>`;
}
