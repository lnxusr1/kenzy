import { html, useState, useEffect } from "./html.js";
import { useFleet, useToasts, dismiss } from "./store.js";
import { FleetView } from "./views/fleet.js";
import { ConfigView } from "./views/config.js";
import { ServicesView } from "./views/services.js";
import { SkillsView } from "./views/skills.js";
import { PeopleView } from "./views/people.js";
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

const NAV = [
  { id: "fleet", label: "Fleet", ico: "▣" },
  { id: "services", label: "Services", ico: "❏" },
  { id: "skills", label: "Skills", ico: "✦" },
  { id: "ha", label: "Home Assistant", ico: "⌂" },
  { id: "people", label: "People", ico: "☺" },
  { id: "schedules", label: "Scheduled", ico: "◷" },
  { id: "activity", label: "Activity", ico: "↗" },
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
  const [view, setView] = useState("fleet");
  const [node, setNode] = useState(null);
  const [svc, setSvc] = useState(null);
  const [personSel, setPersonSel] = useState(null);
  const [navOpen, setNavOpen] = useState(false);
  const active = NAV.find((n) => n.id === view) || NAV[0];
  const title = view === "config" ? "Node config" : active.label;

  const go = (id) => {
    if (id === "services") setSvc(null); // sidebar Services always opens the list
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
          ${NAV.filter((n) => n.id !== "ha" || haOn).map((n) => {
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
          ${view === "config"
            ? html`<${ConfigView} node=${node} onBack=${() => go("fleet")} />`
            : view === "services"
              ? html`<${ServicesView} selected=${svc} onSelect=${setSvc} />`
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
