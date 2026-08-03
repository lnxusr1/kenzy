// Allowed-value sets for config options that take a fixed list, so the editors
// can render a native <select> chooser instead of a freeform text box. Maintained
// alongside the packaged config defaults (which document these in comments).

const LOG_LEVELS = ["debug", "info", "warning", "error"];
const CAPTURE_LEVELS = ["trace", "debug", "info", "warning", "error"];

// Every service has these; keep the wording identical across them. The tls pair
// isn't in any packaged config — the server injects its own cert/key into each
// served service config when it terminates TLS (server.py _effective_service_config).
const LOG_HELP = "How much detail this service prints to its console log.";
const CAPTURE_HELP =
  "How much detail is kept for the dashboard's Logs tab — can be deeper than the console.";
const TLS_CERT_HELP =
  "Certificate this service presents for HTTPS. Filled in from the server automatically when the server uses TLS.";
const TLS_KEY_HELP = "Private key for that certificate. Filled in from the server the same way.";

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
// (node). Kept concise — the docs are the full reference. Every editable key
// must have one: tests/test_dashboard_help.py fails the build otherwise.
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
    "openai.language": "Language code for the cloud provider, or blank to auto-detect.",
    "openai.timeout": "Seconds to wait for the cloud provider before falling back.",
    "openai.fallback": "On cloud failure, silently retry on local whisper.",
    "wyoming.enabled": "Let Home Assistant's voice pipelines transcribe through this service.",
    "wyoming.port": "Port for that Home Assistant listener.",
    log_level: LOG_HELP,
    log_capture_level: CAPTURE_HELP,
    "tls.cert": TLS_CERT_HELP,
    "tls.key": TLS_KEY_HELP,
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
    "wyoming.enabled": "Let Home Assistant's voice pipelines speak with Kenzy's voice.",
    "wyoming.port": "Port for that Home Assistant listener.",
    log_level: LOG_HELP,
    log_capture_level: CAPTURE_HELP,
    "tls.cert": TLS_CERT_HELP,
    "tls.key": TLS_KEY_HELP,
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
    "speaker.url": "Voice-identification endpoint, used when a skill needs to check or enroll a voice (auto-wired from the server; set for multi-host).",
    system_prompt: "The standing instructions that shape how Kenzy answers — her personality and rules.",
    voice_prompt: "Default speaking style passed to the voice, when a reply doesn't set its own.",
    "location.city": "Used for weather and 'near me' answers.",
    "location.state": "State or region, for the same.",
    "location.country": "Country, for the same.",
    "location.timezone": "Timezone for times, alarms and 'what's today'.",
    "location.latitude": "Latitude — sharpens local weather. Blank = use the city name.",
    "location.longitude": "Longitude — sharpens local weather. Blank = use the city name.",
    "skills.dir": "Folder holding your own skills. They override built-ins of the same name.",
    "skills.disabled": "Skills or skill files to switch off. Easier to toggle from the Skills tab.",
    "skills.news.feeds.local": "RSS feeds for local news.",
    "skills.news.feeds.latest": "RSS feeds for top headlines.",
    "skills.news.feeds.world": "RSS feeds for world news.",
    "skills.news.feeds.politics": "RSS feeds for politics.",
    "memory.classifier_keep_alive": "Keep the classifier model loaded in Ollama (e.g. 30m, or -1 forever). Blank = don't ask.",
    log_level: LOG_HELP,
    log_capture_level: CAPTURE_HELP,
    "tls.cert": TLS_CERT_HELP,
    "tls.key": TLS_KEY_HELP,
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
    log_level: LOG_HELP,
    log_capture_level: CAPTURE_HELP,
    "tls.cert": TLS_CERT_HELP,
    "tls.key": TLS_KEY_HELP,
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
  wakeword_models: "Custom wake-word model files (.tflite/.onnx). Blank = the bundled \"Hey Kenzy\".",
  wakeword_threshold: "Wake-word confidence to trigger (0–1). Lower = more sensitive.",
  wakeword_vad_threshold: "Voice-detection gate (0–1); 0 = off. ~0.5 rejects near-silence false wakes.",
  wake_onset_ms: "One-breath commands: hold the ready chime this long after the wake word. Keep talking and the chime is skipped; pause and it plays. 0 = chime instantly.",
  volume_buttons: "USB speakerphone volume buttons: the +/− keys move this room's canonical volume. Needs the mediakeys extra and the input group on the node.",
  volume_button_device: "auto = the endpoint tied to this node's own audio device. Or a stable device-name match — never an eventN path (those change across boots).",
  volume_button_step: "Volume points per press (1–20). Held buttons repeat on devices that report holds.",
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
  // Node-played sounds (bundled name or a path on the NODE; restart to apply).
  sound_ready: "Chime when the wake word fires. Plays even when muted.",
  sound_waiting: "Sound while a request is being processed. Loops until the reply; spoken cues duck over it.",
  sound_connect: "Chime when an intercom call connects.",
  sound_disconnect: "Chime when an intercom call ends.",
  sound_ringback: "Ring loop while calling another room.",
  sound_dialog_end: "Cue when a reply window expires unanswered. Empty = silent.",
  sound_offline: "Cue when the wake word fires but this node can't reach the server. Empty = silent, which is the honest answer — the activation chime is never used here, because it means “I'm listening”.",
  // Server-streamed cues (bundled name or a path on the SERVER host; apply live).
  sound_timer: "Lead-in tone before a timer announcement. Empty = voice only.",
  sound_alarm: "Lead-in tone for each alarm ring; still plays if speech is down. Empty = voice only.",
  sound_error: "Spoken apology when a request fails. Pre-recorded, so it works mid-outage. Empty = silent.",
  sound_thinking: "Spoken “Working on it.” ~5s into a slow answer, over the waiting sound. Add several for variety — one plays at random. Empty = never.",
  sound_working: "Spoken “Still working on it.” ~8s after the first cue ends, if the answer still hasn't started. Also a random pool. Empty = never.",
};

// Node config grouped into logical sections (order preserved). Keys not listed
// fall into "Other".
export const NODE_GROUPS = [
  ["Audio device", ["audio_device", "capture_sample_rate", "playback_sample_rate"]],
  ["Wake word", ["wakeword_models", "wakeword_threshold", "wakeword_vad_threshold", "wake_onset_ms"]],
  [
    "Capture / VAD",
    ["silence_rms_threshold", "vad_enabled", "silence_ms", "speech_min_ms", "no_speech_timeout_ms", "hard_cap_ms"],
  ],
  [
    "Dialog",
    ["hardware_aec", "dialog_no_speech_timeout_ms", "dialog_onset_ms", "dialog_onset_vad_threshold"],
  ],
  ["Speakerphone volume buttons", ["volume_buttons", "volume_button_device", "volume_button_step"]],
  ["Sounds", ["sound_ready", "sound_waiting", "sound_connect", "sound_disconnect", "sound_ringback", "sound_dialog_end", "sound_offline", "sound_timer", "sound_alarm", "sound_error", "sound_thinking", "sound_working"]],
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

// Server settings (the _SERVER_EDITABLE allow-list in server.py). One short line
// per key — enough to act on without opening the docs. Keep every editable key
// covered; tests/test_dashboard_help.py fails the build if one is missing.
export const SERVER_HELP = {
  experimental: "Opt in to features that aren't finished yet. Also marks the dashboard tab so you know which instance you're on.",
  "dashboard.logs": "Enable the Logs and Activity tabs. Off = no transcripts are kept anywhere.",
  "dashboard.controls": "Allow changes from the dashboard. Off = read-only (you can look, not touch).",
  "stt.url": "Where the speech-to-text service lives. Blank = found automatically, or speech recognition off.",
  "stt.timeout": "Seconds to wait for a transcription before giving up.",
  "tts.url": "Where the text-to-speech service lives. Blank = found automatically, or Kenzy stays silent.",
  "tts.timeout": "Seconds to wait for synthesized speech before giving up.",
  "llm.url": "Where the assistant service lives. Blank = found automatically, or no assistant replies.",
  "llm.timeout": "Seconds to wait for a reply before giving up.",
  "speaker.url": "Where the voice-identification service lives. Blank = found automatically, or everyone is 'unknown'.",
  "speaker.timeout": "Seconds to wait for voice identification before giving up.",
  "dialog.max_turns": "How many back-and-forth turns Kenzy will hold the microphone open for before you need the wake word again.",
  "alarm.ring_repeats": "How many times a firing alarm re-rings before giving up. A wake word stops it sooner.",
  "alarm.ring_interval": "Seconds between alarm re-rings.",
  "streaming.enabled": "Start speaking the first sentence while the rest is still being written — noticeably faster replies. Off = wait for the whole answer first.",
  "discovery.enabled": "Announce this server on the network so nodes find it without being told an address.",
  "discovery.instance": "The name this server announces itself under. Only matters if you run more than one.",
  "integrations.mqtt.enabled": "Publish room state to an MQTT broker so Home Assistant sees your nodes as devices.",
  "integrations.mqtt.host": "Broker address. Username and password are set under API keys, never here.",
  "integrations.mqtt.port": "Broker port (1883 plain, 8883 TLS).",
  "integrations.mqtt.base_topic": "Topic prefix for everything Kenzy publishes.",
  "integrations.mqtt.discovery_prefix": "Home Assistant's discovery prefix. Leave alone unless you changed it in HA.",
  "integrations.mqtt.commands": "Let Home Assistant control nodes (trigger, stop, volume, mute). Off = Kenzy only reports, never obeys.",
  "occupancy.enabled": "Track which rooms have people in them, from your Home Assistant sensors and from who Kenzy hears. Shown on the Presence tab; she doesn't act on it yet.",
  "fleet.offline_alert_minutes": "How long a room may be unreachable before the Fleet page calls it a fault. An orphaned node still answers its wake word, so nobody in the room notices. 0 = never alert.",
  "fleet.restart_grace_minutes": "Quiet period after you restart or upgrade a node, so expected downtime doesn't raise an alert. Nodes also announce a planned shutdown themselves, so an ordinary restart is silent either way.",
};

export function serverHelp(key) {
  return SERVER_HELP[key] || null;
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
    // Label must be exactly the dotted parent: the row renderer only strips the
    // prefix when group === the key's parent, so "TLS" would print "tls.cert"
    // while every other service (grouped by parent) prints "cert". The heading
    // is uppercased by CSS, so this still reads as TLS.
    ["tls", (k) => k.startsWith("tls.")],
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
