import { html, render, useState, useEffect } from "./html.js";
import { getMe, logout } from "./api.js";
import { startPolling, stopPolling } from "./store.js";
import { LoginView } from "./views/login.js";
import { Shell } from "./shell.js";

// Restore saved theme before first paint of components (index.html sets a default).
try {
  const t = localStorage.getItem("kenzy-theme");
  if (t) document.documentElement.setAttribute("data-theme", t);
} catch {
  /* ignore */
}

function App() {
  const [auth, setAuth] = useState(null); // null=checking, true/false
  const [user, setUser] = useState(null);

  useEffect(() => {
    getMe().then((m) => {
      setAuth(!!m.authenticated);
      setUser(m.username);
    });
  }, []);

  useEffect(() => {
    if (auth) {
      startPolling();
      return () => stopPolling();
    }
  }, [auth]);

  if (auth === null) return html`<div class="boot">Connecting…</div>`;
  if (!auth)
    return html`<${LoginView} onLogin=${(u) => {
      setUser(u);
      setAuth(true);
    }} />`;
  return html`<${Shell} user=${user} onLogout=${async () => {
    await logout();
    stopPolling();
    setAuth(false);
  }} />`;
}

render(html`<${App} />`, document.getElementById("app"));
