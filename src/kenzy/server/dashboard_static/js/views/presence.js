// Presence — the v5 occupancy world model (rooms, who was heard, HA socket health).
//
// This view is the point of the 5.0 spine: the tracker's decay curves are only
// tunable if you can watch them being wrong in a real house. Everything shown
// here carries PROVENANCE and AGE on purpose — "occupied (mmWave, 4s ago)" and
// "occupied (voice, 3min ago)" are different claims and must never collapse
// into one boolean. "Unknown" is a real state, not a blank: no sensor and no
// recent voice means we don't know, which is different from empty.
import { html, useState, useEffect } from "../html.js";
import { getPresence } from "../api.js";
import { subscribeSession } from "../store.js";

const STATE_LABEL = {
  occupied: "occupied",
  maybe: "maybe",
  unknown: "unknown",
};

function age(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}

// A sensor entity_id is noise in a glanceable view; the useful part is which
// KIND of evidence is speaking.
function sourceLabel(room) {
  const src = room.source || "";
  if (!src) return "no evidence";
  if (src === "voice") return "heard a voice";
  if (src === "released") return "sensor cleared";
  return src.startsWith("binary_sensor.") ? "sensor" : src;
}

function RoomCard({ room }) {
  const cls = `room-card ${room.state}`;
  return html`<div class=${cls}>
    <div class="room-top">
      <span class="room-name mono">${room.room}</span>
      <span class=${"room-state " + room.state}>${STATE_LABEL[room.state] || room.state}</span>
    </div>
    <div class="room-meter" title=${`confidence ${room.confidence}`}>
      <span style=${`width:${Math.round((room.confidence || 0) * 100)}%`}></span>
    </div>
    <dl class="kv room-kv">
      <dt>evidence</dt><dd>${sourceLabel(room)}</dd>
      <dt>last change</dt><dd>${age(room.age)}</dd>
      ${room.person_name
        ? html`<dt>last heard</dt>
            <dd>${room.person_name} · ${age(room.identity_age)}</dd>`
        : null}
      ${room.stale
        ? html`<dt>note</dt><dd>sensor held, but Home Assistant is unreachable</dd>`
        : null}
    </dl>
  </div>`;
}

export function PresenceView() {
  const [data, setData] = useState(null);

  const load = () => getPresence().then(setData).catch(() => setData({ enabled: false }));
  useEffect(() => {
    load();
    // Rides the existing live channel: a voice session IS occupancy evidence,
    // so refresh the moment one lands. Plus a slow poll, because decay is
    // time-based — the page should visibly fade even when nothing happens.
    const un = subscribeSession(load);
    const t = setInterval(load, 10000);
    return () => {
      un && un();
      clearInterval(t);
    };
  }, []);

  if (!data) return html`<div class="empty">Loading…</div>`;

  if (!data.enabled) {
    return html`<section class="section">
      <header><h2>Presence</h2><span class="rule"></span></header>
      <div class="card pad">
        <p class="micro">
          Occupancy is off, or Home Assistant isn't configured. Kenzy builds this
          picture from your motion/presence sensors and from who she hears in
          each room. Configure Home Assistant under Fleet → llm, and enable
          <span class="mono">occupancy.enabled</span> in Settings.
        </p>
      </div>
    </section>`;
  }

  const src = data.source || {};
  const rooms = data.rooms || [];
  const people = data.people || [];
  const occupied = rooms.filter((r) => r.state === "occupied").length;

  return html`
    <section class="section">
      <header>
        <h2>Presence</h2><span class="rule"></span>
        <span class="micro">${occupied} of ${rooms.length} room${rooms.length === 1 ? "" : "s"} occupied</span>
      </header>
      <div class="card pad">
        <div class="room-grid">
          ${rooms.length
            ? rooms.map((r) => html`<${RoomCard} key=${r.room} room=${r} />`)
            : html`<div class="empty">No rooms yet.</div>`}
        </div>
      </div>
    </section>

    ${people.length
      ? html`<section class="section">
          <header><h2>People</h2><span class="rule"></span></header>
          <div class="card pad">
            <dl class="kv">
              ${people.map(
                (p) => html`<dt>${p.name || p.entity_id}</dt>
                  <dd>${p.home ? "home" : "away"} · ${age(p.age)}</dd>`,
              )}
            </dl>
          </div>
        </section>`
      : null}

    <section class="section">
      <header><h2>Home Assistant feed</h2><span class="rule"></span></header>
      <div class="card pad">
        <dl class="kv">
          <dt>connection</dt>
          <dd class="feed-conn">
            <span class=${"led " + (src.connected && !src.stale ? "up" : src.connected ? "busy" : "down")}></span>
            ${src.connected ? (src.stale ? "connected, but quiet" : "connected") : "disconnected"}
          </dd>
          <dt>last event</dt><dd>${age(src.last_event_age)}</dd>
          <dt>evidence entities</dt><dd>${src.map_entities ?? 0}</dd>
          <dt>events used / seen</dt><dd>${src.emitted ?? 0} / ${src.received ?? 0}</dd>
          ${src.last_error ? html`<dt>last error</dt><dd>${src.last_error}</dd>` : null}
        </dl>
      </div>
    </section>`;
}
