import { html, useState } from "../html.js";
import { login } from "../api.js";

export function LoginView({ onLogin }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setErr("");
    const ok = await login(username, password);
    setBusy(false);
    if (ok) onLogin(username);
    else setErr("Invalid username or password.");
  }

  return html`
    <div class="login">
      <form class="panel" onSubmit=${submit}>
        <div class="brand">
          <span class="wordmark"><span class="glyph"></span><span class="name">Kenzy</span></span>
        </div>
        <h1>Fleet Console</h1>
        <p class="err">${err}</p>
        <div class="field">
          <label for="u">Username</label>
          <input id="u" value=${username} onInput=${(e) => setUsername(e.target.value)}
                 autocomplete="username" autofocus />
        </div>
        <div class="field">
          <label for="p">Password</label>
          <input id="p" type="password" value=${password} onInput=${(e) => setPassword(e.target.value)}
                 autocomplete="current-password" />
        </div>
        <button class="btn-primary" disabled=${busy}>${busy ? "Signing in…" : "Sign in"}</button>
        <p class="hint">Default <code>admin</code> / <code>password</code> — change with <code>kenzy-passwd</code>.</p>
      </form>
    </div>
  `;
}
