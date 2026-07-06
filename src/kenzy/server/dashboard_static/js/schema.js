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

export function nodeEnum(key) {
  return NODE_ENUMS[key] || null;
}

export function serviceEnum(service, path) {
  return (SERVICE_ENUMS[service] || {})[path] || null;
}
