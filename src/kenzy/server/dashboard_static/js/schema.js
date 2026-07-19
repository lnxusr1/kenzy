// Allowed-value sets for config options that take a fixed list, so the editors
// can render a native <select> chooser instead of a freeform text box. Maintained
// alongside the packaged config defaults (which document these in comments).

const LOG_LEVELS = ["debug", "info", "warning", "error"];
const CAPTURE_LEVELS = ["trace", "debug", "info", "warning", "error"];

// Node per-node override keys → allowed values (keyed by the override key).
export const NODE_ENUMS = {
  log_level: LOG_LEVELS,
  log_capture_level: CAPTURE_LEVELS,
};

// Backend service config → allowed values, keyed by dotted path within the config.
export const SERVICE_ENUMS = {
  stt: {
    provider: ["whisper", "openai"],
    "whisper.model": ["tiny", "base", "small", "medium", "large-v2", "large-v3"],
    "whisper.device": ["cpu", "cuda"],
    "whisper.compute_type": ["int8", "float16", "float32"],
    "openai.model": ["gpt-4o-mini-transcribe", "gpt-4o-transcribe", "whisper-1"],
    log_level: LOG_LEVELS,
    log_capture_level: CAPTURE_LEVELS,
  },
  tts: {
    provider: ["openai", "kokoro"],
    "kokoro.device": ["auto", "cpu", "cuda", "mps"],
    log_level: LOG_LEVELS,
    log_capture_level: CAPTURE_LEVELS,
  },
  llm: {
    "skills.web_search.provider": ["duckduckgo", "searxng"],
    // Latency knob: how long the model may "think" before speaking. A voice
    // assistant defaults to none; unsupported providers drop it harmlessly.
    // "" = don't send the parameter at all.
    "params.reasoning_effort": ["", "none", "minimal", "low", "medium", "high"],
    "params.service_tier": ["", "auto", "default", "flex", "priority"],
    log_level: LOG_LEVELS,
    log_capture_level: CAPTURE_LEVELS,
  },
  speaker: { log_level: LOG_LEVELS, log_capture_level: CAPTURE_LEVELS },
};

// One-line help shown under each field. Keyed by dotted path (services) or key
// (node). Kept concise — the docs are the full reference. Missing key = no hint.
export const SERVICE_HELP = {
  stt: {
    host: "Bind address for the STT HTTP service.",
    port: "HTTP port.",
    provider: "Transcription backend. whisper = local; openai = cloud (audio leaves the box).",
    "whisper.model": "faster-whisper size. Bigger = more accurate + slower.",
    "whisper.device": "cpu, or cuda if you have an NVIDIA GPU.",
    "whisper.compute_type": "Numeric precision. int8 for CPU, float16 for a GPU.",
    "whisper.language": "Language code, or blank to auto-detect.",
    "openai.model": "OpenAI transcription model. Needs OPENAI_API_KEY.",
    "openai.fallback": "On cloud failure, silently retry on local whisper.",
  },
  tts: {
    host: "Bind address for the TTS HTTP service.",
    port: "HTTP port.",
    provider: "Speech backend. openai = cloud; kokoro = local (needs the kokoro extra).",
    "openai.model": "OpenAI TTS model.",
    "openai.voice": "Voice persona.",
    "openai.speed": "Playback speed, 0.25–4.0.",
    "openai.fallback": "On cloud failure, silently retry on local Kokoro (if installed).",
    "kokoro.voice": "Kokoro voice id.",
    "kokoro.device": "auto picks CUDA → MPS → CPU.",
    "kokoro.speed": "Playback speed, 0.5–2.0.",
    "kokoro.lang_code": "Language code; derived from the voice if blank.",
  },
  llm: {
    host: "Bind address for the LLM HTTP service.",
    port: "HTTP port.",
    model: "LiteLLM model string (e.g. gpt-5.1, ollama/hermes3).",
    base_url: "Endpoint for a local model or proxy. Blank = the model's own provider.",
    "params.reasoning_effort": "How long the model thinks before replying. Blank = don't send it.",
    "params.service_tier": "OpenAI service tier. priority = paid low-latency. Blank = don't send it.",
    max_tool_iterations: "Max skill-call loops before returning whatever the model has.",
    "memory.enabled": "Per-person memory (the fact ledger). Off = no memory skills, no /memory API.",
    "memory.maintenance_interval": "Seconds between mechanical sweeps (dedupe, expiry). 0 disables.",
    "memory.superseded_keep_days": "How long a superseded fact stays recoverable on disk.",
    "memory.semantic_interval": "Daily backstop for model-driven consolidation (the real trigger is each write). 0 disables it entirely.",
    "memory.semantic_cooldown": "Min seconds between consolidation model runs — bursts of writes coalesce into one call.",
    "memory.private_to_cloud": "Off (default): private facts never enter a CLOUD model's context or consolidation — they still answer by voice. On: allow them through.",
    "memory.classifier_model": "Judges new memories for secrets (release / vault / split). Blank = the service model; a CLOUD model is never consulted — set a local one to resolve ambiguity automatically.",
    "memory.classifier_url": "The classifier model's endpoint (Ollama etc.), when set.",
    "skills.web_search.provider": "duckduckgo (keyless) or searxng (self-hosted, private).",
    "skills.web_search.max_results": "Results returned per search.",
    "skills.web_search.timeout": "Search request timeout (seconds).",
    "skills.web_search.region": "DuckDuckGo region code (wt-wt = no region).",
    "skills.web_search.searxng_url": "Your SearXNG /search endpoint.",
    "skills.news.max_headlines": "Headlines returned per request.",
    "skills.home_assistant.url": "Home Assistant base URL.",
    "skills.home_assistant.default_room": "Room assumed when none is spoken.",
    "skills.home_assistant.thermo_min": "Lower comfort clamp (°F) for relative temp changes.",
    "skills.home_assistant.thermo_max": "Upper comfort clamp (°F) for relative temp changes.",
    "skills.home_assistant.cache_ttl": "Seconds to cache the HA topology pull.",
  },
  speaker: {
    host: "Bind address for the speaker-ID HTTP service.",
    port: "HTTP port.",
    model_source: "HuggingFace speaker-embedding model id.",
    model_save_dir: "Local cache dir for the downloaded model.",
    embeddings_dir: "Folder where enrolled voice profiles are saved.",
    identify_threshold: "Voice-match strictness (0–1). Lower accepts more matches but risks mistakes.",
    unknown_speaker: "Name returned when no enrolled speaker matches.",
    "tts.url": "TTS endpoint for speaking enrollment prompts (auto-wired from the server; set for multi-host).",
    allow_voice_enroll: "Let anyone in earshot enroll by voice. Off is safer.",
    enroll_sample_rate: "Recording rate in Hz (16000 is standard).",
    enroll_silence_rms: "Loudness (0–32767) below which counts as silence and ends the recording.",
    enroll_silence_ms: "Milliseconds of silence that end an enrollment recording.",
    enroll_min_speech_ms: "Minimum milliseconds of speech for a usable enrollment sample.",
    enroll_prompts: "Sentences read aloud during voice enrollment (one per sample).",
  },
};

// Fields shown only when a controlling field has a given value (progressive
// disclosure). A field whose dotted path starts with `prefix` is visible only
// when the current value of `when` equals `is`.
export const SERVICE_DEPS = {
  stt: [
    { when: "provider", is: "whisper", prefix: "whisper." },
    { when: "provider", is: "openai", prefix: "openai." },
  ],
  tts: [
    { when: "provider", is: "openai", prefix: "openai." },
    { when: "provider", is: "kokoro", prefix: "kokoro." },
  ],
  llm: [
    { when: "skills.web_search.provider", is: "searxng", prefix: "skills.web_search.searxng_url" },
    { when: "skills.web_search.provider", is: "duckduckgo", prefix: "skills.web_search.region" },
  ],
};

export const NODE_HELP = {
  room_id: "The room's friendly name — server-owned, sent to the assistant as context.",
  audio_device: "ALSA device. Blank = system default.",
  wakeword_threshold: "Wake-word confidence to trigger (0–1). Lower = more sensitive.",
  wakeword_vad_threshold: "Voice-detection gate (0–1); 0 = off. ~0.5 rejects near-silence false wakes.",
  silence_rms_threshold: "Loudness (0–32767) below which counts as silence.",
  vad_enabled: "Off = stream until the server says stop (no voice-activity endpointing).",
  silence_ms: "Consecutive silence that ends a capture (after speech).",
  speech_min_ms: "Minimum speech before silence can end a capture.",
  no_speech_timeout_ms: "Give up if no speech is heard after activation.",
  hard_cap_ms: "Absolute capture length ceiling (VAD mode).",
  dialog_no_speech_timeout_ms: "Milliseconds Kenzy waits for your reply before ending the dialog.",
  dialog_onset_ms: "Milliseconds of speech needed to start a reply turn (rejects clinks).",
  dialog_onset_vad_threshold: "Voice-detection strictness (0–1) for starting a reply. Higher = firmer.",
  hardware_aec: "Declare the speaker has echo cancellation. Off = strict turn-taking (no barge-in/intercom).",
  volume: "Playback volume 0–100. Persists.",
  capture_sample_rate: "Mic rate; set to the device's native rate if 16 kHz is unsupported.",
  playback_sample_rate: "Speaker rate; set to native if 24 kHz is unsupported.",
  log_level: "Console log level.",
  log_capture_level: "How deep the dashboard log viewer captures.",
};

// Node config grouped into logical sections (order preserved). Keys not listed
// fall into "Other".
export const NODE_GROUPS = [
  ["Audio device", ["audio_device", "capture_sample_rate", "playback_sample_rate"]],
  ["Wake word", ["wakeword_models", "wakeword_threshold", "wakeword_vad_threshold"]],
  [
    "Capture / VAD",
    ["silence_rms_threshold", "vad_enabled", "silence_ms", "speech_min_ms", "no_speech_timeout_ms", "hard_cap_ms"],
  ],
  [
    "Dialog",
    ["hardware_aec", "dialog_no_speech_timeout_ms", "dialog_onset_ms", "dialog_onset_vad_threshold"],
  ],
  ["Sounds", ["sound_ready", "sound_waiting", "sound_connect", "sound_disconnect", "sound_ringback", "sound_dialog_end", "sound_timer", "sound_alarm", "sound_error"]],
  ["Playback", ["volume"]],
  ["Logging", ["log_level", "log_capture_level", "verbose"]],
];

export function nodeEnum(key) {
  return NODE_ENUMS[key] || null;
}

export function serviceEnum(service, path) {
  return (SERVICE_ENUMS[service] || {})[path] || null;
}

export function serviceHelp(service, path) {
  return (SERVICE_HELP[service] || {})[path] || null;
}

export function nodeHelp(key) {
  return NODE_HELP[key] || null;
}

// Semantic sections for editors whose keys don't group cleanly by dotted parent
// (flat keys, or backends we want combined). Each entry is [label, predicate].
export const SERVICE_SECTIONS = {
  speaker: [
    ["General", (k) => ["host", "port", "log_level", "log_capture_level"].includes(k)],
    ["Peer services", (k) => k.endsWith(".url") || k.endsWith(".timeout")],
    ["Model", (k) => k.startsWith("model_")],
    ["Identification", (k) => ["identify_threshold", "unknown_speaker"].includes(k)],
    ["Enroll", (k) => k.startsWith("enroll_") || k === "allow_voice_enroll" || k === "embeddings_dir"],
  ],
};

export const SERVER_SECTIONS = [
  ["Server", (k) => !k.includes(".")], // top-level keys (experimental, …)
  ["Dashboard", (k) => k.startsWith("dashboard.")],
  ["Backend services", (k) => ["stt.", "tts.", "llm.", "speaker."].some((p) => k.startsWith(p))],
  ["Dialog & alarms", (k) => k.startsWith("dialog.") || k.startsWith("alarm.")],
  ["Discovery", (k) => k.startsWith("discovery.")],
  ["Integrations", (k) => k.startsWith("integrations.")],
];

// Group items into ordered named sections (predicate on each item's key); items
// matching no section fall into a trailing "Other" bucket. `keyOf` extracts the
// key when items aren't plain strings (e.g. the server settings' field objects).
export function groupBySections(items, sections, keyOf = (x) => x) {
  const buckets = sections.map(([label]) => [label, []]);
  const other = [];
  for (const it of items) {
    const key = keyOf(it);
    const i = sections.findIndex(([, pred]) => pred(key));
    if (i === -1) other.push(it);
    else buckets[i][1].push(it);
  }
  const res = buckets.filter(([, ks]) => ks.length);
  if (other.length) res.push(["Other", other]);
  return res;
}

// Group a flat list of dotted keys by their parent path ("General" for top-level).
export function groupByParent(keys) {
  const groups = new Map();
  for (const k of keys) {
    const i = k.lastIndexOf(".");
    const g = i === -1 ? "General" : k.slice(0, i);
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(k);
  }
  // General first, then the rest in first-seen order.
  return [...groups.entries()].sort((a, b) =>
    a[0] === "General" ? -1 : b[0] === "General" ? 1 : 0,
  );
}

// Is a field visible given the current values and the service's dependency map?
// A gated field (its path matches a dep's prefix) shows only when the controlling
// field equals the required value; ungated fields are always visible.
export function fieldVisible(service, key, vals) {
  for (const d of SERVICE_DEPS[service] || []) {
    const matches = d.prefix.endsWith(".") ? key.startsWith(d.prefix) : key === d.prefix;
    if (matches) return vals[d.when] === d.is;
  }
  return true;
}
