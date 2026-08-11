import { html, useState, useEffect } from "./html.js";
import { useFleet, useToasts, dismiss } from "./store.js";
import { AddonView } from "./addons.js";
import { FleetView } from "./views/fleet.js";
import { ConfigView } from "./views/config.js";
import { ServicesView } from "./views/services.js";
import { SkillsView } from "./views/skills.js";
import { PeopleView } from "./views/people.js";
import { PresenceView } from "./views/presence.js";
import { ProactiveView } from "./views/proactive.js";
import { SchedulesView } from "./views/schedules.js";
import { HaView } from "./views/ha.js";
import { ActivityView } from "./views/activity.js";
import { LogsView } from "./views/logs.js";
import { SettingsView } from "./views/settings.js";

function Toasts() {
  const toasts = useToasts();
  return html`<div class="toasts">
    ${toasts.map(
      (t) => html`
        <div key=${t.id} class=${"toast " + t.kind} onClick=${() => dismiss(t.id)} role="status">
          <span class=${"led " + (t.kind === "ok" ? "up" : "down")}></span>
          <span>${t.text}</span>
        </div>
      `,
    )}
  </div>`;
}

// Ordered in three bands, most-glanced first, moving from the HOME to the
// SOFTWARE: what's happening now (fleet health, who's where, what she just did,
// what's coming) → the household she knows (people, the house, her abilities) →
// the machinery (backends, debugging, settings). Fleet stays first: it is the
// default view and the wordmark's target.
const NAV = [
  // Now
  { id: "fleet", label: "Fleet", ico: "▣" },
  { id: "presence", label: "Presence", ico: "◎" },
  { id: "proactive", label: "Proactive", ico: "◬" },
  { id: "activity", label: "Activity", ico: "↗" },
  { id: "schedules", label: "Scheduled", ico: "◷" },
  // Household
  { id: "people", label: "People", ico: "☺" },
  { id: "ha", label: "Home Assistant", ico: "⌂" },
  { id: "skills", label: "Skills", ico: "✦" },
  // System. NB there is deliberately no "Services" entry: a service is
  // configured by clicking its chip on Fleet, the same way a node is — Fleet is
  // the fleet. The `services` ROUTE still exists to render that editor.
  { id: "logs", label: "Logs", ico: "≡" },
  { id: "settings", label: "Settings", ico: "⚙" },
];

function ThemeToggle() {
  const [theme, setTheme] = useState(
    () => document.documentElement.getAttribute("data-theme") || "dark",
  );
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    try {
      localStorage.setItem("kenzy-theme", theme);
    } catch {
      /* ignore */
    }
  }, [theme]);
  return html`
    <button class="btn-ghost" onClick=${() => setTheme(theme === "dark" ? "light" : "dark")}
            title="Toggle theme">${theme === "dark" ? "◐ Light" : "◑ Dark"}</button>
  `;
}

function ago(ts) {
  const s = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ago` : `${Math.floor(m / 60)}h ago`;
}

function ConnPill() {
  const { error, updatedAt, live } = useFleet();
  const [, tick] = useState(0);
  // Re-render once a second so the relative time keeps counting between updates.
  useEffect(() => {
    const t = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, []);
  const ok = !error && updatedAt;
  const label = !ok ? "offline" : `${live ? "live" : "polling"} · ${ago(updatedAt)}`;
  return html`<span class="pill"><span class=${"led " + (ok ? (live ? "up" : "busy") : "down")}></span>
    ${label}</span>`;
}

export function Shell({ user, onLogout }) {
  const { data } = useFleet();
  const logsOn = !!(data && data.flags && data.flags.logs);
  // No-HA households see no HA surfaces (flag = HA configured OR the app
  // front door in use, and the home_assistant module not disabled). Absent
  // flag (older server) keeps the tab.
  const haOn = !data || !data.flags || data.flags.ha_active !== false;
  // Presence needs a live tracker AND its tuning editor (which lives in the HA
  // tab) — a picture you cannot correct is worse than no tab. Absent flag
  // (older server) keeps it, same convention as ha_active above.
  const presenceOn = !data || !data.flags || data.flags.occupancy_active !== false;
  // Proactive is shown whenever the gate EXISTS — including switched off. The
  // point of the page is that "she's been silent for months" is visible, so
  // hiding it when disabled would hide the very thing worth seeing.
  const proactiveOn = !data || !data.flags || data.flags.proactive_active !== false;
  // 5.1 add-ons: installed plugins that ship a panel. Empty list ⇒ no band,
  // no routes, no cost — nothing here is hand-wired per feature.
  const addons = (data && data.flags && data.flags.addons) || [];
  // Installed-but-refused plugins still surface here: "I installed it and
  // nothing appeared" is exactly the moment someone scans the nav, so a
  // fault-only state must not render nothing. The entry leads to Settings,
  // where the Add-ons card states the reason and the fix.
  const addonFaults = (data && data.flags && data.flags.addon_faults) || [];
  const [view, setView] = useState("fleet");
  const [node, setNode] = useState(null);
  const [svc, setSvc] = useState(null);
  const [personSel, setPersonSel] = useState(null);
  const [navOpen, setNavOpen] = useState(false);
  const activeAddon = view.startsWith("addon:")
    ? addons.find((a) => "addon:" + a.id === view)
    : null;
  const active = NAV.find((n) => n.id === view) || NAV[0];
  const title = view === "config" ? "Node config" : activeAddon ? activeAddon.label : active.label;

  const go = (id) => {
    if (id === "people") setPersonSel(null); // likewise People: nav = back to the list
    setView(id);
    setNavOpen(false);
  };
  const configure = (id) => {
    setNode(id);
    setView("config");
  };
  const configureService = (name) => {
    setSvc(name);
    setView("services");
    setNavOpen(false);
  };

  return html`
    <div class=${"shell" + (navOpen ? " nav-open" : "")}>
      <${Toasts} />
      <div class="scrim" onClick=${() => setNavOpen(false)}></div>
      <aside class="sidebar">
        <div class="brand"><a href="#" class="wordmark" aria-label="Kenzy — go to Fleet"
          onClick=${(e) => { e.preventDefault(); go("fleet"); }}><span class="glyph"></span><span class="name">Kenzy</span></a></div>
        <nav class="nav">
          ${NAV.filter((n) => (n.id !== "ha" || haOn) && (n.id !== "presence" || presenceOn) && (n.id !== "proactive" || proactiveOn)).map((n) => {
            // Logs and Activity are gated by the server's `dashboard.logs` flag
            // (Activity records carry transcripts, like logs).
            const disabled = (n.id === "logs" || n.id === "activity") && !logsOn;
            return html`
              <a key=${n.id} href="#" aria-disabled=${disabled ? "true" : "false"}
                 class=${n.id === view || (view === "config" && n.id === "fleet") ? "active" : ""}
                 onClick=${(e) => {
                   e.preventDefault();
                   if (!disabled) go(n.id);
                 }}>
                <span class="ico">${n.ico}</span>${n.label}
                ${disabled ? html`<span class="micro" style="margin-left:auto">off</span>` : null}
              </a>
            `;
          })}
          ${addons.length || addonFaults.length
            ? html`<div class="nav-band">Add-ons</div>
                ${addons.map(
                  (a) => html`
                    <a key=${"addon:" + a.id} href="#"
                       class=${"addon:" + a.id === view ? "active" : ""}
                       onClick=${(e) => {
                         e.preventDefault();
                         go("addon:" + a.id);
                       }}>
                      <span class="ico">${a.ico || "◈"}</span>${a.label}
                    </a>
                  `,
                )}
                ${addonFaults.length
                  ? html`<a href="#" class="nav-fault"
                       title="An installed add-on could not be loaded — details in Settings"
                       onClick=${(e) => {
                         e.preventDefault();
                         go("settings");
                       }}>
                      <span class="ico">⚠</span>${addonFaults.length}${" "}not loaded
                    </a>`
                  : null}`
            : null}
        </nav>
        <div class="foot">
          <div class="userline">
            <span class="avatar">${(user || "?").slice(0, 1).toUpperCase()}</span>
            <span class="who"><b>${user || "—"}</b><span class="micro">signed in</span></span>
          </div>
          <div class="row-actions">
            <${ThemeToggle} />
            <button class="btn-ghost" onClick=${onLogout}>Sign out</button>
          </div>
        </div>
      </aside>

      <div class="main">
        <header class="topbar">
          <button class="hamburger" onClick=${() => setNavOpen(true)} aria-label="Open menu">≡</button>
          <h1>${title}</h1>
          <span class="spacer"></span>
          <${ConnPill} />
        </header>
        <main class="content">
          ${activeAddon
            ? html`<${AddonView} addon=${activeAddon} />`
          : view === "config"
            ? html`<${ConfigView} node=${node} onBack=${() => go("fleet")} />`
            : view === "services"
              ? html`<${ServicesView} selected=${svc}
                  onSelect=${(name) => {
                    setSvc(name);
                    if (!name) setView("fleet"); // Back → the fleet, not a dead list
                  }} />`
              : view === "presence"
                ? html`<${PresenceView} />`
              : view === "proactive"
                ? html`<${ProactiveView} />`
              : view === "skills"
                ? html`<${SkillsView} />`
                : view === "ha"
                  ? html`<${HaView} />`
                  : view === "people"
                    ? html`<${PeopleView} selected=${personSel} onSelect=${setPersonSel}
                        onConfigureService=${configureService} />`
                  : view === "schedules"
                    ? html`<${SchedulesView} />`
                  : view === "activity"
                    ? html`<${ActivityView} />`
                    : view === "logs"
                      ? html`<${LogsView} />`
                      : view === "settings"
                        ? html`<${SettingsView} onLogout=${onLogout} />`
                        : html`<${FleetView} onConfigure=${configure}
                            onConfigureService=${configureService} />`}
        </main>
      </div>
    </div>
  `;
}
