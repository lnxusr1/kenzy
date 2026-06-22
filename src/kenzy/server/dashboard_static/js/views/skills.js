import { html, useState, useEffect } from "../html.js";
import { getSkills } from "../api.js";
import { send, notify } from "../store.js";

// Skill registry view: the skills + fast intents loaded by kenzy-llm, with
// invocation counts and a live enable/disable toggle (persisted to the llm
// service override and applied without a restart).
export function SkillsView() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");

  const load = () =>
    getSkills()
      .then(setData)
      .catch((e) => setErr(String(e)));

  useEffect(() => {
    load();
  }, []);

  if (err) return html`<div class="empty">Could not load skills: ${err}</div>`;
  if (!data) return html`<div class="empty">Loading…</div>`;
  if (!data.reachable)
    return html`<div class="empty">The LLM service isn't reachable, so its skills can't be
      listed. Check <span class="mono">llm.url</span> and that <span class="mono">kenzy-llm</span>
      is running.</div>`;

  const controls = data.controls;
  const skills = data.skills || [];
  const fast = data.fast_intents || [];
  const enabledN = skills.filter((s) => !s.disabled).length;

  async function toggle(name, disabled) {
    setBusy(name);
    const res = await send("set_skill_disabled", { name, disabled });
    if (res.ok) {
      await load();
      notify(`${name} ${disabled ? "disabled" : "enabled"}.`);
    } else {
      notify(res.error || "Could not change the skill.", "err");
    }
    setBusy("");
  }

  const toggleBtn = (s) =>
    html`<button class=${"btn-ghost sk-toggle" + (s.disabled ? " off" : "")}
      disabled=${!controls || busy === s.name}
      title=${controls ? "" : "enable dashboard.controls to toggle"}
      onClick=${() => toggle(s.name, !s.disabled)}>
      ${busy === s.name ? "…" : s.disabled ? "Enable" : "Disable"}</button>`;

  return html`
    <div class="stats">
      <div class="tile"><div class="micro">Skills loaded</div><div class="k">${skills.length}</div></div>
      <div class="tile"><div class="micro">Enabled</div><div class="k">${enabledN}</div></div>
      <div class="tile"><div class="micro">Fast intents</div><div class="k">${fast.length}</div></div>
    </div>

    <section class="section">
      <header><h2>Skills</h2><span class="rule"></span></header>
      ${!controls
        ? html`<p class="micro">Read-only — set <span class="mono">dashboard.controls: true</span>
            to enable/disable skills from here.</p>`
        : null}
      <div class="card">
        <div class="sk-list">
          ${skills.map(
            (s) => html`
              <div class=${"sk-row" + (s.disabled ? " disabled" : "")} key=${s.name}>
                <div class="sk-main">
                  <div class="sk-name">
                    <span class="mono">${s.name}</span>
                    ${s.fast ? html`<span class="badge fast">fast</span>` : null}
                    ${s.disabled ? html`<span class="badge off">disabled</span>` : null}
                  </div>
                  <div class="sk-desc micro">${s.description || "—"}</div>
                </div>
                <div class="sk-meta">
                  <span class="micro" title="invocations since the service started">${s.calls} call${s.calls === 1 ? "" : "s"}</span>
                  ${toggleBtn(s)}
                </div>
              </div>
            `,
          )}
        </div>
      </div>
    </section>

    ${fast.length
      ? html`<section class="section">
          <header><h2>Fast intents</h2><span class="rule"></span></header>
          <p class="micro">Deterministic matchers that run before the LLM, in descending
            priority. Disabling a fast intent's skill disables both.</p>
          <div class="card pad">
            <dl class="kv">
              ${fast.map(
                (f) => html`<dt>
                    <span class="mono">${f.name}</span>
                    ${f.disabled ? html` <span class="badge off">disabled</span>` : null}
                  </dt>
                  <dd class="micro">priority ${f.priority} · ${f.calls} call${f.calls === 1 ? "" : "s"}</dd>`,
              )}
            </dl>
          </div>
        </section>`
      : null}`;
}
