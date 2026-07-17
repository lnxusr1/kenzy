// Thin wrappers over the dashboard's JSON API. Cookies (set by login) ride
// along automatically on same-origin requests.

export async function getMe() {
  try {
    const r = await fetch("/api/me");
    return r.ok ? await r.json() : { authenticated: false };
  } catch {
    return { authenticated: false };
  }
}

export async function login(username, password) {
  // Credentials go in an Authorization: Basic header (no request body, since the
  // server reads the WS-handshake-style request via the websockets HTTP hook).
  const basic = "Basic " + btoa(unescape(encodeURIComponent(`${username}:${password}`)));
  const r = await fetch("/api/login", { headers: { Authorization: basic } });
  return r.ok;
}

export async function logout() {
  try {
    await fetch("/api/logout");
  } catch {
    /* ignore */
  }
}

export async function getState() {
  const r = await fetch("/api/state");
  if (!r.ok) throw new Error(`state ${r.status}`);
  return await r.json();
}

export async function getSettings() {
  const r = await fetch("/api/settings");
  if (!r.ok) throw new Error(`settings ${r.status}`);
  return await r.json();
}

export async function getSkills() {
  const r = await fetch("/api/skills");
  if (!r.ok) throw new Error(`skills ${r.status}`);
  return await r.json();
}

export async function getPeople() {
  const r = await fetch("/api/people");
  if (!r.ok) throw new Error(`people ${r.status}`);
  return await r.json();
}

export async function getSchedules() {
  const r = await fetch("/api/schedules");
  if (!r.ok) throw new Error(`schedules ${r.status}`);
  return await r.json();
}

export async function getHaCuration() {
  const r = await fetch("/api/ha/curation");
  if (!r.ok) throw new Error(`ha curation ${r.status}`);
  return await r.json();
}
