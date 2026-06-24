# LLM Configuration

**File:** `configs/llm.yaml`  
**Command:** `kenzy-llm [config_path]`

The LLM service processes transcribed text through a tool-calling loop and returns a spoken response. It uses [LiteLLM](https://github.com/BerriAI/litellm) as the model abstraction layer, so any provider LiteLLM supports works out of the box.

!!! note "Pulled from the server"
    `kenzy-llm` pulls this config from the server at boot — it discovers the server via mDNS (or `KENZY_SERVER_URL`) and blocks until it answers, so start the server first. Edit it from the dashboard's **Services** tab (writes `configs/services/llm.yaml` on the server and restarts the service). Passing an explicit path loads locally instead (dev/offline). See [central config for backend services](server.md#central-config-for-backend-services).

## Full reference

### Core

| Key | Default | Description |
|---|---|---|
| `host` | `"127.0.0.1"` | Bind address |
| `port` | `8766` | HTTP port |
| `log_level` | `"info"` | What the service prints to its console |
| `log_capture_level` | `"debug"` | How deep the dashboard log viewer can see, independent of `log_level` |
| `model` | `"gpt-4o"` | LiteLLM model string (see [Model strings](#model-strings)) |
| `base_url` | — | Provider base URL. Required for Ollama, LM Studio, and similar local providers. |
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
    device_ids_yaml: "data/home_assistant/device_ids.yaml"
    device_ids_json: "data/home_assistant/device_ids.json"
    default_room: "living_room"
```

!!! tip "Using a local model"
    To run entirely offline with Ollama:
    ```yaml
    model: "ollama/hermes3"
    base_url: "http://localhost:11434"
    ```
    No API key is required. Skill sub-calls (news summaries, HA resolution) also use this model unless overridden with a per-skill `model` key.
