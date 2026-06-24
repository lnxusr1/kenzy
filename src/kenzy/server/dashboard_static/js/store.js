// Fleet store. Primary feed is the live WebSocket (/ws); if it drops we fall back
// to polling /api/state and keep retrying the socket.
import { useState, useEffect } from "preact/hooks";
import { getState } from "./api.js";

let _state = { data: null, loading: true, error: null, live: false, updatedAt: null };
const _subs = new Set();

let _running = false;
let _ws = null;
let _poll = null;
let _retry = null;
let _seq = 0;
const _pending = new Map();
// Live calibration samples ({type:"tune", node, sample}) are pushed to subscribers
// (the calibration panel), not into the fleet state.
const _tuneSubs = new Set();

// Subscribe to live tune samples; returns an unsubscribe function.
export function subscribeTune(fn) {
  _tuneSubs.add(fn);
  return () => _tuneSubs.delete(fn);
}

// Live pipeline-session records (Activity tab).
const _sessionSubs = new Set();
export function subscribeSession(fn) {
  _sessionSubs.add(fn);
  return () => _sessionSubs.delete(fn);
}

// Send a mutation over the WS and resolve with the server's {ok,error} ack.
export function send(type, payload = {}) {
  return new Promise((resolve) => {
    if (!_ws || _ws.readyState !== WebSocket.OPEN) {
      resolve({ ok: false, error: "not connected — is the live channel up?" });
      return;
    }
    const id = "m" + ++_seq;
    _pending.set(id, resolve);
    _ws.send(JSON.stringify({ id, type, ...payload }));
    setTimeout(() => {
      if (_pending.has(id)) {
        _pending.delete(id);
        resolve({ ok: false, error: "timed out" });
      }
    }, 6000);
  });
}

function emit(patch) {
  _state = { ..._state, ...patch };
  _subs.forEach((fn) => fn(_state));
}

export async function refresh() {
  try {
    emit({ data: await getState(), loading: false, error: null, updatedAt: Date.now() });
  } catch (e) {
    emit({ loading: false, error: String(e) });
  }
}

function startPoll() {
  if (_poll || !_running) return;
  refresh();
  _poll = setInterval(refresh, 3000);
}
function stopPoll() {
  clearInterval(_poll);
  _poll = null;
}

function connectWS() {
  if (!_running) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  let ws;
  try {
    ws = new WebSocket(`${proto}://${location.host}/ws`);
  } catch {
    return startPoll();
  }
  _ws = ws;
  ws.onopen = () => stopPoll(); // live feed — no need to poll
  ws.onmessage = (ev) => {
    try {
      const m = JSON.parse(ev.data);
      if (m.type === "ack") {
        const r = _pending.get(m.id);
        if (r) {
          _pending.delete(m.id);
          r(m);
        }
      } else if (m.type === "state") {
        emit({ data: m.data, loading: false, error: null, live: true, updatedAt: Date.now() });
      } else if (m.type === "tune") {
        _tuneSubs.forEach((fn) => fn(m));
      } else if (m.type === "session") {
        _sessionSubs.forEach((fn) => fn(m.data));
      }
    } catch {
      /* ignore */
    }
  };
  const drop = () => {
    if (_ws === ws) _ws = null;
    if (!_running) return;
    emit({ live: false });
    startPoll(); // fall back…
    clearTimeout(_retry);
    _retry = setTimeout(connectWS, 5000); // …and keep trying the socket
  };
  ws.onclose = drop;
  ws.onerror = drop;
}

export function startPolling() {
  if (_running) return;
  _running = true;
  connectWS();
  startPoll(); // immediate data until the socket opens
}

export function stopPolling() {
  _running = false;
  stopPoll();
  clearTimeout(_retry);
  if (_ws) {
    _ws.close();
    _ws = null;
  }
}

export function useFleet() {
  const [s, setS] = useState(_state);
  useEffect(() => {
    _subs.add(setS);
    return () => _subs.delete(setS);
  }, []);
  return s;
}

// ---- Toasts (transient action feedback) -----------------------------------
let _toasts = [];
let _toastSeq = 0;
const _toastSubs = new Set();

export function notify(text, kind = "ok") {
  const id = ++_toastSeq;
  _toasts = [..._toasts, { id, text, kind }];
  _toastSubs.forEach((f) => f(_toasts));
  setTimeout(() => dismiss(id), 3800);
  return id;
}

export function dismiss(id) {
  _toasts = _toasts.filter((t) => t.id !== id);
  _toastSubs.forEach((f) => f(_toasts));
}

export function useToasts() {
  const [t, setT] = useState(_toasts);
  useEffect(() => {
    _toastSubs.add(setT);
    return () => _toastSubs.delete(setT);
  }, []);
  return t;
}
