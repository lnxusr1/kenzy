"use strict";

const $ = (id) => document.getElementById(id);

function pill(text, cls) {
  return `<span class="pill ${cls}">${text}</span>`;
}

function renderNodes(nodes) {
  const tbody = $("nodes").querySelector("tbody");
  if (!nodes.length) {
    tbody.innerHTML = `<tr><td colspan="3" class="muted">no nodes connected</td></tr>`;
    return;
  }
  tbody.innerHTML = nodes.map((n) => {
    const state = n.streaming ? pill("streaming", "streaming") : pill("idle", "idle");
    const sid = n.session_id ? n.session_id.slice(0, 8) : "—";
    return `<tr><td>${n.room_id}</td><td>${state}</td><td class="muted">${sid}</td></tr>`;
  }).join("");
}

function renderServices(services) {
  const tbody = $("services").querySelector("tbody");
  if (!services.length) {
    tbody.innerHTML = `<tr><td colspan="3" class="muted">no services configured</td></tr>`;
    return;
  }
  tbody.innerHTML = services.map((s) => {
    const status = s.up ? pill("up", "up") : pill("down", "down");
    const detail = Object.entries(s.detail || {})
      .filter(([k]) => k !== "status")
      .map(([k, v]) => `${k}=${v}`).join(" ") || "—";
    return `<tr><td>${s.name}</td><td>${status}</td><td class="muted">${detail}</td></tr>`;
  }).join("");
}

async function refresh() {
  try {
    const r = await fetch("api/state");
    if (!r.ok) throw new Error(r.status);
    const state = await r.json();
    renderNodes(state.nodes);
    renderServices(state.services);
    $("status").textContent = "live · " + new Date().toLocaleTimeString();
  } catch (e) {
    $("status").textContent = "disconnected";
  }
}

refresh();
setInterval(refresh, 2000);
