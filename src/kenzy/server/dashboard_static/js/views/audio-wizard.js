// Guided audio setup for one node. The DEVICE step is the one thing that needs
// a human at the dashboard; everything after is the SERVER-driven calibration
// session (the same one "Hey Kenzy, calibrate" runs) watched live: the browser
// renders the prompts/phases from calibration events (mode="silent" — the node
// beeps for the AEC probe instead of speaking), the meter rides the tune relay,
// and results (thresholds, AEC verdict, live wake verify) arrive as events.
import { html, useState, useEffect, useRef } from "../html.js";
import { send, notify, useFleet, subscribeTune, subscribeCalibration } from "../store.js";
import { Meter, logPct, round2 } from "./calibrate.js";

const fmt = (v) => (v === undefined || v === null || v === "" ? "default" : v);

export function AudioWizard({ node, info, onClose, onApplied }) {
  const [step, setStep] = useState("overview");
  const [devSel, setDevSel] = useState("");
  // 5.0.4: when the chosen device also carries volume buttons, the device step
  // offers them here — the same breath as "which audio device", rather than a
  // separate hunt through the key grid. Defaults ON: the buttons are what a
  // speakerphone's owner expects, and the key is one click away either way.
  const [wantKeys, setWantKeys] = useState(true);
  const [saving, setSaving] = useState(false);
  const [devWait, setDevWait] = useState(false); // waiting out the device restart
  const sawDrop = useRef(false);
  const fleet = useFleet();

  // ---- live calibration session state (rendered from server events) ----
  const [running, setRunning] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [phase, setPhase] = useState(null); // quiet|wake|restarting|verify|null
  const [count, setCount] = useState(0);
  const [wakes, setWakes] = useState({ count: 0, target: 4 });
  const [notes, setNotes] = useState([]);
  const [result, setResult] = useState(null); // {patch, kept, verdict}
  const [aec, setAec] = useState(null); // {aec, changed}
  const [verify, setVerify] = useState(null); // {ok, nudges} once resolved
  const [done, setDone] = useState(null); // {ok, summary?}
  const [latest, setLatest] = useState({ rms: 0, wake: 0 });

  const devices = (info.devices || []).filter((d) => d.suggested);
  const cur = (k) => info.config[k];

  useEffect(
    () =>
      subscribeTune((m) => {
        if (m.node !== node || !m.sample || m.sample.stopped) return;
        setLatest({ rms: Number(m.sample.rms) || 0, wake: Number(m.sample.wake) || 0 });
      }),
    [node],
  );

  useEffect(
    () =>
      subscribeCalibration(async (m) => {
        if (m.node !== node) return;
        const e = m.event || {};
        if (!running && e.stage && !done) {
          // A session we didn't start (e.g. voice-initiated) — watch it live.
          setRunning(true);
          setStep("calibrate");
          send("tune_watch", { node });
        }
        if (e.stage === "prompt") setPrompt(e.text || "");
        else if (e.stage === "quiet") {
          setPhase("quiet");
          setCount(Math.round(e.seconds || 6));
        } else if (e.stage === "wake") {
          setPhase("wake");
          setCount(Math.round(e.seconds || 20));
          setWakes((w) => ({ ...w, target: e.target || 4 }));
        } else if (e.stage === "wake_heard") setWakes((w) => ({ ...w, count: e.count || 0 }));
        else if (e.stage === "note") setNotes((n) => [...n, e.text || ""]);
        else if (e.stage === "aec") setAec(e);
        else if (e.stage === "applied") {
          setPhase(null);
          setResult(e);
          if (onApplied) onApplied();
        } else if (e.stage === "restarting") setPhase("restarting");
        else if (e.stage === "verify") setPhase("verify");
        else if (e.stage === "verify_result") setVerify(e);
        else if (e.stage === "done") {
          setPhase(null);
          setRunning(false);
          setDone(e);
        }
      }),
    [node, running, done],
  );

  // Local countdown ticker for the timed phases.
  useEffect(() => {
    if (phase !== "quiet" && phase !== "wake") return undefined;
    const t = setInterval(() => setCount((c) => (c > 0 ? c - 1 : 0)), 1000);
    return () => clearInterval(t);
  }, [phase]);

  async function startCalibration() {
    setPrompt("");
    setNotes([]);
    setResult(null);
    setAec(null);
    setVerify(null);
    setDone(null);
    setWakes({ count: 0, target: 4 });
    const res = await send("calibrate_start", { node });
    if (!res.ok) {
      notify(res.error || "Could not start calibration.", "err");
      return;
    }
    setRunning(true);
  }

  async function cancelCalibration() {
    await send("calibrate_cancel", { node });
    setRunning(false);
    setPhase(null);
    setDone({ ok: false, summary: "cancelled" });
  }

  // ---- device step (unchanged mechanics; flows into calibrate on reconnect) ----
  useEffect(() => {
    if (!devWait) return undefined;
    const n = (fleet.data?.nodes || []).find((x) => x.node_id === node);
    const connected = !!(n && n.connected);
    if (!connected) sawDrop.current = true;
    else if (sawDrop.current) {
      setDevWait(false);
      setStep("calibrate");
    }
    return undefined;
  }, [fleet, devWait]);
  useEffect(() => {
    if (!devWait) return undefined;
    const t = setTimeout(() => {
      setDevWait(false);
      notify("The node is taking a while to come back — check its card.", "err");
      setStep("overview");
    }, 35000);
    return () => clearTimeout(t);
  }, [devWait]);

  async function applyDevice() {
    const d = devices.find((x) => String(x.index) === String(devSel));
    if (!d) return;
    setSaving(true);
    const base = { ...(info.override || {}) };
    delete base.room_id; // server-managed; rejected by set_override
    const cfg = {
      ...base,
      audio_device: d.suggested.audio_device,
      capture_sample_rate: d.suggested.capture_sample_rate,
      playback_sample_rate: d.suggested.playback_sample_rate,
    };
    // Volume buttons ride the same write: auto resolves them from the device
    // we just picked, so no second setting to chase.
    if (d.volume_keys) {
      cfg.volume_buttons = !!wantKeys;
      // Record the EXACT endpoint we just identified rather than "auto":
      // auto can't resolve an alias like "default", and refuses when several
      // devices qualify — so punting here silently disabled the feature the
      // moment the audio device changed.
      if (wantKeys && d.volume_key_device) cfg.volume_button_device = d.volume_key_device;
    }
    const res = await send("set_override", { node, config: cfg });
    setSaving(false);
    if (!res.ok) {
      notify(res.error || "Could not save.", "err");
      return;
    }
    if (onApplied) onApplied();
    await send("restart", { node });
    sawDrop.current = false;
    setDevWait(true);
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
        <dt>echo cancel (AEC)</dt><dd class="mono">${fmt(cur("hardware_aec"))}</dd>
      </dl>
      <p class="micro">Full setup checks the device, then the node runs a guided calibration
        (instructions appear here; it beeps once to test echo cancellation). You can also just
        say <b>“Hey Kenzy, calibrate”</b> in the room.</p>
      <div class="wiz-actions">
        <button class="btn-primary" onClick=${() => setStep("device")}>Start full setup</button>
        <button class="btn-ghost" onClick=${() => setStep("device")}>Device only</button>
        <button class="btn-ghost" onClick=${() => setStep("calibrate")}>Calibrate only</button>
      </div>`;
  }

  function deviceStep() {
    if (devWait) {
      return html`<p>Restarting <strong>${info.room || node}</strong> to apply the device…
        <span class="micro">waiting for it to reconnect.</span></p>
        <div class="wiz-actions"><span class="spinner"></span></div>`;
    }
    return html`
      <p class="micro">Pick the room's mic/speaker — the one setting that needs a human.
        Changing it restarts the node; calibration follows automatically.</p>
      ${devices.length
        ? html`<select class="audio-select" disabled=${saving}
            value=${devSel} onChange=${(e) => setDevSel(e.target.value)}>
            <option value="">choose a device…</option>
            ${devices.map((d) => html`<option value=${d.index}>${d.name}</option>`)}
          </select>`
        : html`<p class="micro">No selectable devices reported by this node.</p>`}
      ${(() => {
        const d = devices.find((x) => String(x.index) === String(devSel));
        if (!d || !d.volume_keys) return null;
        return html`<label class="wiz-opt">
          <input type="checkbox" disabled=${saving} checked=${wantKeys}
            onChange=${(e) => setWantKeys(e.target.checked)} />
          <span>This device has volume buttons — let them change this room's volume</span>
        </label>`;
      })()}
      <p class="audio-current">Current: <span class="mono">${fmt(cur("audio_device"))}</span></p>
      ${wizFooter(
        html`<button class=${devSel ? "btn-primary" : "btn-ghost"} disabled=${!devSel || saving}
          onClick=${applyDevice}>${saving ? "Saving…" : "Apply & restart"}</button>
        <button class=${devSel ? "btn-ghost" : "btn-primary"}
          onClick=${() => setStep("calibrate")}>Keep current →</button>`,
      )}`;
  }

  function calibrateStep() {
    if (done) {
      const v = result && result.verdict;
      return html`
        <p>${done.ok ? "Calibration complete." : `Calibration stopped${done.summary ? ` — ${done.summary}` : ""}.`}</p>
        ${result && Object.keys(result.patch || {}).length
          ? html`<dl class="wiz-cur">${Object.entries(result.patch).map(
              ([k, val]) => html`<dt class="mono">${k}</dt><dd class="mono">${val}</dd>`,
            )}</dl>`
          : null}
        ${aec && aec.changed
          ? html`<p class="micro">Echo cancellation detected as${" "}
              <b>${aec.aec ? "present" : "absent"}</b> — <span class="mono">hardware_aec</span>${" "}
              set to <span class="mono">${String(aec.aec)}</span>.</p>`
          : null}
        ${result && (result.kept || []).length
          ? html`<p class="micro wiz-note">Couldn't auto-calibrate ${result.kept.join(", ")} —
              previous value${result.kept.length > 1 ? "s" : ""} kept.</p>`
          : null}
        ${v
          ? html`<p class="micro">Noise-to-speech separation:${" "}
              <b class=${"sep-" + v}>${v}</b></p>`
          : null}
        ${verify
          ? html`<p class="micro">${verify.ok
              ? "Wake word verified live. ✓"
              : "Wake word could not be verified."}</p>`
          : null}
        <div class="wiz-actions">
          <button class="btn-ghost" onClick=${startCalibration}>Run again</button>
          <button class="btn-primary" onClick=${onClose}>Close</button>
        </div>`;
    }
    if (!running) {
      return html`
        <p class="wiz-hint">The node runs the same guided flow as “Hey Kenzy, calibrate” —
          instructions show here, it beeps once (echo-cancellation test), measures the quiet
          room, then listens for the wake word. About half a minute.</p>
        ${wizFooter(html`<button class="btn-primary" onClick=${startCalibration}>
          Start — then follow the prompts</button>`)}`;
    }
    const phaseUi =
      phase === "quiet"
        ? html`<p class="wiz-hint">🤫 Stay quiet — measuring the room…
            <span class="wiz-count mono">${count}</span></p>`
        : phase === "wake"
          ? html`<p class="wiz-hint">🗣 Say <b>“Hey Kenzy”</b> — heard <b>${wakes.count}</b>
              of ${wakes.target} <span class="wiz-count mono">${count}</span></p>`
          : phase === "restarting"
            ? html`<p class="wiz-hint">Restarting the node to apply… <span class="spinner"></span></p>`
            : phase === "verify"
              ? html`<p class="wiz-hint">🗣 Say <b>“Hey Kenzy”</b>, then <b>“never mind”</b> —
                  this time she's really listening.</p>`
              : html`<p class="wiz-hint">${prompt || "Starting…"}</p>`;
    return html`
      ${phaseUi}
      ${prompt && phase ? html`<p class="micro">${prompt}</p>` : null}
      <${Meter} pct=${logPct(latest.rms)} marks=${[]} />
      <p class="calib-read">level <span class="mono">${Math.round(latest.rms)}</span>
        · wake score <span class="mono">${round2(latest.wake)}</span></p>
      ${aec && aec.changed
        ? html`<p class="micro">Echo cancellation: <b>${aec.aec ? "present" : "absent"}</b>
            — updated.</p>`
        : null}
      ${notes.map((t) => html`<p class="micro wiz-note">${t}</p>`)}
      ${wizFooter(html`<button class="btn-ghost" onClick=${cancelCalibration}>Cancel</button>`)}`;
  }

  function wizFooter(actions) {
    return html`<div class="wiz-footer">
      <button class="btn-ghost" onClick=${() => setStep("overview")}>← Steps</button>
      <div class="wiz-footer-actions">${actions}</div>
    </div>`;
  }

  // Connectivity is judged LIVE (fleet feed), and never ejects the wizard while
  // a session is running — the session itself restarts the node mid-flow.
  const fleetNode = (fleet.data?.nodes || []).find((x) => x.node_id === node);
  const connected = fleetNode ? !!fleetNode.connected : !!info.connected;

  let body;
  if (!connected && !running && !devWait && !done)
    body = html`<p>Node must be connected to set up audio.</p>`;
  else if (!info.controls)
    body = html`<p>Enable <code class="mono">dashboard.controls</code> to use the wizard.</p>`;
  else if (step === "device") body = deviceStep();
  else if (step === "calibrate") body = calibrateStep();
  else body = overview();

  const titles = {
    overview: "Audio setup",
    device: "Step 1 — Audio device",
    calibrate: "Step 2 — Calibration",
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
