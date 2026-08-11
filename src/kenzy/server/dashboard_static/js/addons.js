// Add-on panel host (5.1). A plugin ships plain-ESM panel files as package
// data, served at /addons/<id>/; the shell lazy-imports the entry module on
// first visit and renders its default export. The panel receives the core
// primitives as props (one framework on the page — the vendored ESM), and may
// also import them itself by absolute path ("/js/html.js", "/js/api.js",
// "/js/store.js") since the import map is page-global.
//
// Contract notes for panel authors (the hard-won ones):
// - Mutations go through store.send(type, payload) — NEVER name a payload key
//   `id` (it clobbers the request/ack correlation id and the call times out).
// - A panel is JS in an authenticated admin session; it loads only from an
//   installed distribution's package data. Treat that trust accordingly.
import { html, useEffect, useState } from "./html.js";
import * as api from "./api.js";
import * as store from "./store.js";

export function AddonView({ addon }) {
  const [Panel, setPanel] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    let alive = true;
    setPanel(null);
    setErr(null);
    import(addon.panel)
      .then((mod) => {
        if (!alive) return;
        if (typeof mod.default !== "function") {
          setErr("panel module has no default-exported component");
        } else {
          setPanel(() => mod.default);
        }
      })
      .catch((e) => {
        if (alive) setErr(String(e));
      });
    return () => {
      alive = false;
    };
  }, [addon.panel]);
  if (err) {
    // The failure names itself — a blank pane sends the operator log-diving.
    return html`<div class="card">
      <b>${addon.label}</b> panel failed to load: <span class="micro">${err}</span>
    </div>`;
  }
  if (!Panel) return html`<div class="micro">Loading ${addon.label}…</div>`;
  return html`<${Panel} html=${html} api=${api} store=${store} addon=${addon} />`;
}
