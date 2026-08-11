// The sample panel: the smallest legal add-on panel. Default-exports a
// component; receives the core primitives as props (html, api, store, addon).
export default function SamplePanel({ html, addon }) {
  return html`<div class="card">
    <b>${addon.label}</b> — the sample add-on panel rendered (v${addon.version}).
  </div>`;
}
