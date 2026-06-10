# Skills

Skills are the mechanism by which Kenzy takes actions in the world — fetching weather, reading news, controlling smart home devices, and more. Each skill is an async Python function decorated with `@skill` in the `skills/` directory. The LLM calls skills as tools based on their docstrings and type signatures.

## How skills work

At startup, the LLM service scans the `skills/` directory, imports every `.py` file, and registers all functions decorated with `@skill`. The decorator introspects each function's signature, type annotations, and docstring to generate the JSON Schema tool definition that LiteLLM passes to the model. No separate configuration or registration step is needed.

```
skills/
  weather.py       ← defines get_current_weather(), get_forecast()
  news.py          ← defines get_news(), get_news_article()
  home_assistant.py ← defines handle_home_control()
  my_skill.py      ← your custom skill
```

The LLM decides which skills to call and with what arguments based entirely on the docstrings. Well-written docstrings that describe *when* to use a skill are as important as the skill's implementation.

## Skill configuration

Per-skill settings live under `skills.<skill_name>` in `llm.yaml`:

```yaml
skills:
  weather:
    units: imperial
    default_location: "New York, NY"

  home_assistant:
    url: "http://homeassistant.local:8123"
```

Skills read these values at runtime via `get_config(section, key, default)`:

```python
from kenzy.llm.skills import get_config

units = get_config("weather", "units", "imperial")
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
