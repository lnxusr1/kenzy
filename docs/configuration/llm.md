# Setting Up the Language Model

**File:** `configs/llm.yaml`  
**Command:** `kenzy-llm [config_path]`

The language model is Kenzy's "thinking" — and **whose computer it thinks on
is your call**. The default is a cloud model because it's the fastest way to
a working setup (one key, no downloads, runs beside everything else on a
small box), but it's a means to an end, not the point: Kenzy speaks
[LiteLLM](https://github.com/BerriAI/litellm), so any provider works — 
including one running entirely in your house.

The two keys that matter:

```yaml
model: "gpt-5-mini"                    # which model does the thinking
# base_url: "http://127.0.0.1:11434"   # where it lives (only for local/self-hosted)
```

- **Cloud quick-start** — set `model` to an OpenAI (`gpt-5-mini`), Anthropic
  (`claude-sonnet-5`), or OpenRouter (`openrouter/…`) model string and give
  Kenzy that provider's API key (below). Only the transcribed *text* of each
  request goes to the provider — with the default local speech recognition,
  your audio never leaves home.
- **Your own hardware, no third party** — run [Ollama](https://ollama.com) or
  LM Studio and point `model`/`base_url` at it. The full walkthrough,
  including honest hardware guidance, is
  [Running Fully Local](../fully-local.md#configuring-the-brain-the-llm).
- **Both** — a cloud primary with a local `fallback` model keeps working
  through an internet outage (see the reference below).

!!! tip "API keys, if you're new to them"
    An API key is a password that lets Kenzy use an account you hold with a
    provider. You create it in the provider's account pages (OpenAI:
    *platform.openai.com → API keys*; Anthropic: *console.anthropic.com*;
    OpenRouter: *openrouter.ai → Keys*), copy it once, and paste it into
    Kenzy's dashboard under **Settings → API keys** (or `~/.config/kenzy/.env`).
    Kenzy stores it on your server, never displays it again, and never sends
    it anywhere except to that provider.

!!! note "Pulled from the server"
    `kenzy-llm` pulls this config from the server at boot — it discovers the server via mDNS (or `KENZY_SERVER_URL`) and blocks until it answers, so start the server first. Edit it from the dashboard's **Services** tab (writes `configs/services/llm.yaml` on the server and restarts the service). Passing an explicit path loads locally instead (dev/offline). See [central config for backend services](server.md#central-config-for-backend-services).

## Model strings

LiteLLM encodes the provider in the model string prefix:

| Provider | Example model string |
|---|---|
| OpenAI | `gpt-4o`, `gpt-4o-mini` |
| Anthropic | `claude-opus-4-8`, `claude-sonnet-4-6` |
| Ollama (local) | `ollama/hermes3`, `ollama/llama3.1` |
| LM Studio (local) | `openai/model-name` + `base_url` |
| Together AI | `together_ai/meta-llama/Llama-3-70b` |

API keys are read automatically from the environment. See the [LiteLLM provider docs](https://docs.litellm.ai/docs/providers) for the required environment variable per provider.

## Example

```yaml
host: "127.0.0.1"
port: 8766

model: "gpt-4o"
max_tool_iterations: 5

system_prompt: |
  You are Kenzy, a helpful home assistant. Be concise and conversational.

voice_prompt: "Speak in a friendly, natural tone at a moderate pace."

location:
  city: "Raleigh"
  state: "NC"
  country: "US"
  timezone: "America/New_York"
  latitude: 35.7796
  longitude: -78.6382

skills:
  dir: skills
  disabled: []

  weather:
    units: imperial

  home_assistant:
    url: "http://homeassistant.local:8123"
    curation_file: "data/home_assistant/curation.yaml"   # optional
    default_room: "living_room"
```

!!! tip "Using a local model"
    To run entirely offline with Ollama:
    ```yaml
    model: "ollama/hermes3"
    base_url: "http://localhost:11434"
    ```
    No API key is required. Skill sub-calls (news summaries, HA resolution) also use this model unless overridden with a per-skill `model` key.

!!! note "Custom endpoints never receive a cloud provider's API key"
    `base_url` redirects model calls to a server you choose. Requests to it deliberately carry **none** of the provider keys from your environment (`OPENAI_API_KEY` etc.) — so a changed or mistyped endpoint can never leak a cloud credential. Local providers (Ollama, LM Studio) need no key and just work. If your custom endpoint is a **hosted proxy that requires auth** (a LiteLLM proxy, OpenRouter), put its key in `CUSTOM_LLM_API_KEY` in `.env` — that is the one credential ever sent to a `base_url`. (If you previously kept such a proxy key in `OPENAI_API_KEY`, move it there.)

## Advanced

Everything below is the full reference — useful when you're tuning; skippable when you're starting.

### Full reference

### Core

| Key | Default | Description |
|---|---|---|
| `host` | `"127.0.0.1"` | Bind address |
| `port` | `8766` | HTTP port |
| `log_level` | `"info"` | What the service prints to its console |
| `log_capture_level` | `"debug"` | How deep the dashboard log viewer can see, independent of `log_level` |
| `model` | `"gpt-5.1"` *(shipped config)* | LiteLLM model string (see [Model strings](#model-strings)) |
| `base_url` | — | Provider base URL. Required for Ollama, LM Studio, and similar local providers. |
| `fallback.model` | — | Optional **local fallback**: when the primary model call fails (cloud outage, provider error), the request is silently retried once against this model — e.g. `"ollama/qwen2.5:14b"`. If the fallback also fails, the user just hears the error cue. Unset = no fallback. |
| `fallback.base_url` | — | The fallback model's endpoint, e.g. `"http://127.0.0.1:11434"` |
| `params.reasoning_effort` | `""` | How long the model may "think" before speaking. Empty = **don't send the parameter** (models with adaptive defaults, like gpt-5.1, gain nothing from an explicit value). Set `none`…`high` to force a level on models whose default reasoning is heavier. Ignored harmlessly by providers that don't support it. |
| `params.service_tier` | `""` | OpenAI service tier — `"priority"` is the paid low-latency tier if your account has it. Empty = don't send. |
| `params.*` | — | Anything else LiteLLM accepts (`service_tier: "priority"` for OpenAI's paid low-latency tier, `temperature`, `max_tokens`, …), merged into every model call. Unsupported parameters are dropped per-provider. Credential/routing keys (`api_key`, `base_url`, …) are ignored here by design. |
| `max_tool_iterations` | `5` | Maximum skill call iterations per request before returning whatever the model has |

### Prompts

| Key | Default | Description |
|---|---|---|
| `system_prompt` | *(built-in)* | Injected at the start of every LLM call. Defines Kenzy's persona and behavior. |
| `voice_prompt` | *(built-in)* | Fallback TTS style instruction used when the model does not provide one. |

### Location context

Injected into every LLM call and used as the default by location-aware skills (e.g. weather).

| Key | Description |
|---|---|
| `location.city` | City name |
| `location.state` | State or province |
| `location.country` | Country code (e.g. `"US"`) |
| `location.timezone` | IANA timezone (e.g. `"America/New_York"`) |
| `location.latitude` | Decimal latitude |
| `location.longitude` | Decimal longitude |

### Skills

| Key | Default | Description |
|---|---|---|
| `skills.dir` | `"skills"` | Your skills **overlay** directory, loaded in addition to the bundled built-in skills. Relative paths resolve under the config home — `~/.config/kenzy/skills`, or the repo root in a dev checkout. |
| `skills.disabled` | `[]` | Skill function names to disable (applies to built-in and overlay skills alike) without deleting any file. |

Per-skill configuration lives under `skills.<skill_name>` as a nested map. See [Built-in Skills](../skills/builtin.md) for the keys each skill accepts.

### Memory

Per-person [memory](../memory.md) — the fact ledger and its upkeep jobs (job
history is visible on the service's token-gated `GET /jobs`).

| Key | Default | Description |
|---|---|---|
| `memory.enabled` | `true` | The whole memory feature. `false` ⇒ no ledger, no memory skills, the `/memory` endpoints answer 503, and the dashboard's memory surfaces say so. |
| `memory.file` | `"data/memory/facts.jsonl"` | The ledger file, config-home-relative — plain JSONL, human-readable, rides backups. |
| `memory.maintenance_interval` | `3600` | Seconds between mechanical sweeps (expired facts, exact duplicates, superseded tombstones past their keep window). No model involved. `0` disables. |
| `memory.superseded_keep_days` | `30` | How long a superseded fact stays on disk (recoverable) before the sweep removes it. |
| `memory.semantic_interval` | `86400` | The daily backstop for **semantic consolidation** (merging restatements with your configured model). The real trigger is each "remember…" — this catches anything a failed run left behind. `0` disables the semantic layer entirely. |
| `memory.semantic_cooldown` | `30` | Rate limit between model-driven consolidation runs — dictating five facts in a row costs one model call, not five. |
| `memory.private_to_cloud` | `false` | By default, **private**-tier facts are withheld from a *cloud* model's context and consolidation (they still answer by voice, and consolidate on a local model). `true` opts out of the protection. |
| `memory.classifier_model` | *(blank)* | The write-path classifier's model — it judges whether a new memory contains a secret (release / vault to the lockbox / split). Blank = the service model, **but a cloud model is never consulted for this**: without a local model, ambiguous writes are held for dashboard review instead. Point this at a local model (e.g. `ollama/qwen3:8b`) to resolve ambiguity automatically. |
| `memory.classifier_url` | *(blank)* | The classifier model's endpoint (when `classifier_model` is set). |
