# Skills

Skills are the mechanism by which Kenzy takes actions in the world — fetching weather, reading news, controlling smart home devices, and more. Each skill is an async Python function decorated with `@skill`. The LLM calls skills as tools based on their docstrings and type signatures.

## How skills work

Skills load from two places at LLM-service startup:

1. **Built-in skills** ship inside the package (`kenzy/llm/builtin_skills/`) and always load first.
2. **Your overlay** — the directory named by `skills.dir` in `llm.yaml` (default `skills/` under your config home: `~/.config/kenzy/skills`, or the repo root in a dev checkout). Loaded second, so a file here that defines a skill of the **same name** overrides the built-in one.

Every `.py` file is imported and all `@skill`-decorated functions are registered. The decorator introspects each function's signature, type annotations, and docstring to generate the JSON Schema tool definition that LiteLLM passes to the model — no separate configuration or registration step. Disable any skill (built-in or custom) by its function name under `skills.disabled` in `llm.yaml`.

```
kenzy/llm/builtin_skills/   ← bundled: weather, news, home_assistant, stocks, …
~/.config/kenzy/skills/
  my_skill.py               ← your custom skill (overrides a built-in of the same name)
```

The LLM decides which skills to call and with what arguments based entirely on the docstrings. Well-written docstrings that describe *when* to use a skill are as important as the skill's implementation.

## Two resolution tiers

Kenzy resolves a request in two stages, fastest first:

1. **Fast path (deterministic).** Before the LLM is consulted, the request is run through any registered **fast intents** — `@fast_intent` matchers that recognise common, high-frequency commands locally and answer instantly with **no remote model call**. Time/date queries and the bulk of Home Assistant control ("turn on the kitchen lights") are handled here.
2. **LLM fallback.** Anything a fast intent doesn't confidently match falls through to the tool-calling LLM exactly as before. The fast layer is deliberately *high-precision*: when it isn't sure, it defers.

A skill can expose **both** — a deterministic `@fast_intent` front-end for the easy cases and an `@skill` tool definition for the LLM to fall back on. See [Writing Skills](writing-skills.md#fast-intents-deterministic-path) for the fast-intent API.

## Skill configuration

Per-skill settings live under `skills.<skill_name>` in `llm.yaml`:

```yaml
skills:
  home_assistant:
    url: "http://homeassistant.local:8123"

  news:
    max_headlines: 5
```

Skills read these values at runtime via `get_config(section, key, default)`:

```python
from kenzy.llm.skills import get_config

url = get_config("home_assistant", "url", "http://homeassistant.local:8123")
```

## Disabling skills

List skill function names under `skills.disabled` in `llm.yaml` to disable them without deleting the file:

```yaml
skills:
  disabled:
    - get_stock_info
    - yes_no_maybe
```

## Secrets

Skills read API keys from environment variables (`.env`). Never hardcode secrets in skill files.

## Included skills

See [Built-in Skills](builtin.md) for documentation on all skills that ship with Kenzy.

## Writing a skill

See [Writing Skills](writing-skills.md) for a step-by-step guide.
