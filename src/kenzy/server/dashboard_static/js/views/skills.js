import { html, useState, useEffect } from "../html.js";
import { getSkills } from "../api.js";
import { send, notify } from "../store.js";

// Blast-radius hints per MODULE: what else turns off when a whole module is
// disabled, shown on the group header so the operator sees it BEFORE toggling
// (the HA screen goes dormant and lists are hard-gated on home_assistant).
const BLAST_RADIUS = {
  home_assistant: "the Home Assistant screen (curation) and shopping/to-do lists",
};

// Module dependencies: dep must be enabled for the module to actually work
// (lists are the voice layer over HA's todo entities — the runtime hard-gates
// them on home_assistant). Shown on the group header; when the dep is off the
// group is marked inactive even if its own toggle is on.
const DEPENDS = {
  lists: "home_assistant",
};

// Friendlier group titles for module (file) names.
function moduleTitle(m) {
  return (m || "other").replace(/_skill$/, "").replace(/_/g, " ");
}

// Skill registry view: the skills + fast intents loaded by kenzy-llm, with
// invocation counts and a live enable/disable toggle (persisted to the llm
// service override and applied without a restart).
export function SkillsView() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");
  const [open, setOpen] = useState({}); // module → expanded (accordion)

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
  const modules = data.modules || [];
  const enabledN = skills.filter((s) => !s.disabled).length;

  // Group skills by their source module (the unit that means a feature — there
  // is no skill literally named "home_assistant"; the module toggle is how you
  // turn that whole feature off).
  const groups = {};
  for (const s of skills) (groups[s.module || "other"] = groups[s.module || "other"] || []).push(s);
  const moduleDisabled = Object.fromEntries(modules.map((m) => [m.name, m.disabled]));
  const fastCount = {};
  for (const f of fast) fastCount[f.module || "other"] = (fastCount[f.module || "other"] || 0) + 1;

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
      ${Object.keys(groups)
        .sort()
        .map((mod) => {
          const members = groups[mod];
          const modOff = !!moduleDisabled[mod];
          const dep = DEPENDS[mod];
          const depOff = dep && !!moduleDisabled[dep];
          const isOpen = !!open[mod];
          return html`
            <div class="card sk-group" key=${mod}>
              <div class=${"sk-row sk-group-head" + (modOff || depOff ? " disabled" : "")}
                role="button" tabindex="0" aria-expanded=${isOpen}
                onClick=${() => setOpen({ ...open, [mod]: !isOpen })}
                onKeyDown=${(e) => { if (e.key === "Enter" || e.key === " ") setOpen({ ...open, [mod]: !isOpen }); }}>
                <div class="sk-main">
                  <div class="sk-name">
                    <span class="sk-chev" aria-hidden="true">${isOpen ? "▾" : "▸"}</span>
                    <strong>${moduleTitle(mod)}</strong>
                    <span class="micro mono">${mod}.py</span>
                    <span class="micro">${members.length} skill${members.length === 1 ? "" : "s"}${fastCount[mod] ? ` · ${fastCount[mod]} fast` : ""}</span>
                    ${modOff ? html`<span class="badge off">disabled</span>` : null}
                    ${!modOff && depOff
                      ? html`<span class="badge off" title=${`enabled, but inert while ${dep} is disabled`}>inactive — ${moduleTitle(dep)} is off</span>`
                      : null}
                  </div>
                  ${BLAST_RADIUS[mod]
                    ? html`<div class="sk-desc micro"><em>Also powers: ${BLAST_RADIUS[mod]}</em></div>`
                    : null}
                  ${dep
                    ? html`<div class="sk-desc micro"><em>Requires: ${moduleTitle(dep)}</em> — these skills use Home Assistant's to-do entities as their storage</div>`
                    : null}
                </div>
                <div class="sk-meta">
                  <button class=${"btn-ghost sk-toggle" + (modOff ? " off" : "")}
                    disabled=${!controls || busy === mod}
                    title=${controls
                      ? "toggles every skill and fast intent in this module"
                      : "enable dashboard.controls to toggle"}
                    onClick=${(e) => { e.stopPropagation(); toggle(mod, !modOff); }}>
                    ${busy === mod ? "…" : modOff ? "Enable all" : "Disable all"}</button>
                </div>
              </div>
              ${isOpen ? html`<div class="sk-list">
                ${members.map(
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
              </div>` : null}
            </div>
          `;
        })}
    </section>

    ${fast.length
      ? html`<section class="section">
          <header><h2>Fast intents</h2><span class="rule"></span></header>
          <p class="micro">Deterministic matchers that run before the LLM, in descending
            priority. Disabling a module (the "Disable all" group toggle) disables its
            fast intents too.</p>
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
