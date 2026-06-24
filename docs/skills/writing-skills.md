# Writing Skills

A skill is an async Python function decorated with `@skill`. Drop the file in your skills overlay directory — `~/.config/kenzy/skills/` (or `skills/` in a dev checkout), set by `skills.dir` in `llm.yaml` — and it is loaded automatically at startup, alongside the built-in skills. A file that defines a skill of the same name as a built-in overrides it.

## Minimal example

```python
# ~/.config/kenzy/skills/my_skill.py
from kenzy.llm.skills import skill

@skill
async def greet_user(name: str) -> str:
    """Greet a user by name.

    Use when the user asks to be greeted or says hello and provides their name.

    name: the person's name to include in the greeting
    """
    return f"Hello, {name}! Great to meet you."
```

## The `@skill` decorator

The decorator does three things:

1. Validates that the function is async
2. Reads the function signature and type annotations to generate a JSON Schema
3. Registers the function in the skill registry under its `__name__`

No imports or config entries are needed beyond the decorator.

## Writing good docstrings

The LLM reads the docstring to decide **when** to call your skill and how to populate its arguments. A vague docstring leads to the skill being called at the wrong time or not at all.

**Good docstring structure:**

```python
@skill
async def get_current_weather(location: str) -> str:
    """Get the current weather conditions for a location.

    Use when the user asks about the weather, temperature, or conditions —
    e.g. "what's the weather like?", "is it raining in Seattle?",
    "how cold is it outside?".

    Do NOT use for forecasts — use get_forecast instead.

    location: city and state or full address, e.g. "Burlington, NC"
    """
```

Key elements:

- **First line**: one-sentence summary of what the skill does
- **When to use**: explicit examples of utterances that should trigger this skill
- **When NOT to use**: if there is ambiguity with another skill, be explicit
- **Parameter descriptions**: one line per parameter explaining expected format

## Type annotations

The decorator maps Python types to JSON Schema:

| Python type | JSON Schema |
|---|---|
| `str` | `{"type": "string"}` |
| `int` | `{"type": "integer"}` |
| `float` | `{"type": "number"}` |
| `bool` | `{"type": "boolean"}` |
| `list[str]` | `{"type": "array", "items": {"type": "string"}}` |
| `Literal["a", "b"]` | `{"type": "string", "enum": ["a", "b"]}` |
| `str \| None` (Optional) | `{"type": "string"}` — parameter is not required |

Parameters with a default value are not marked as required in the schema.

## Reading configuration

Skills access per-skill settings from `llm.yaml` via `get_config`:

```python
from kenzy.llm.skills import get_config, skill

@skill
async def my_skill(query: str) -> str:
    """..."""
    api_url = get_config("my_skill", "url", "http://localhost:8080")
    timeout  = float(get_config("my_skill", "timeout", 10.0))
    ...
```

In `llm.yaml`:

```yaml
skills:
  my_skill:
    url: "http://my-service:8080"
    timeout: 15.0
```

## Reading secrets

Use `os.environ` or `os.getenv`. Never hardcode credentials.

```python
import os

token = os.environ.get("MY_SERVICE_TOKEN", "")
if not token:
    return "My service is not configured — set MY_SERVICE_TOKEN in .env"
```

## Making HTTP requests

Use `httpx.AsyncClient` for all outbound HTTP calls:

```python
import httpx

async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
    resp = await client.get("https://api.example.com/data")
    resp.raise_for_status()
    data = resp.json()
```

## Calling a sub-LLM

For tasks that require reasoning over fetched content (article summarization, device resolution), call LiteLLM directly:

```python
from litellm import acompletion
from kenzy.llm.skills import get_config

async def _summarize(content: str) -> str:
    model    = get_config("my_skill", "model") or "gpt-4o"
    base_url = get_config("my_skill", "base_url") or None

    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Summarize the following in 3 sentences."},
            {"role": "user",   "content": content},
        ],
    }
    if base_url:
        kwargs["base_url"] = base_url

    response = await acompletion(**kwargs)
    return response.choices[0].message.content or ""
```

## Running blocking code

Use `asyncio.get_running_loop().run_in_executor` for synchronous operations (file I/O, third-party sync libraries):

```python
import asyncio

loop = asyncio.get_running_loop()
result = await loop.run_in_executor(None, my_sync_function, arg1, arg2)
```

## Error handling

Return a human-readable string on failure — the LLM will relay it as a spoken response:

```python
@skill
async def my_skill(query: str) -> str:
    """..."""
    try:
        return await _do_work(query)
    except Exception as exc:
        log.error("my_skill failed: %s", exc, exc_info=True)
        return f"I wasn't able to complete that: {exc}"
```

## Fast intents (deterministic path)

An `@skill` is invoked by the LLM after a remote model round-trip. For common, high-frequency commands that should feel **instant**, add a deterministic `@fast_intent` matcher that runs *before* the LLM and answers with no model call.

```python
# skills/datetime_skill.py
import datetime
from kenzy.llm.skills import FastResult, fast_intent

@fast_intent(priority=100)
async def fast_datetime(utterance: str, room_id: str | None, speaker: str | None) -> FastResult:
    """Answer time/date questions instantly, no LLM."""
    text = utterance.lower()
    if "time" not in text or "what" not in text:
        return FastResult.miss()          # defer to the next matcher / the LLM
    now = datetime.datetime.now()
    return FastResult.handled(f"It's {now.strftime('%-I:%M %p')}.")
```

A matcher is called as `func(utterance, room_id, speaker)` and must return a `FastResult`:

| Constructor | Effect |
|---|---|
| `FastResult.handled(text, voice_prompt=None, expect_response=False)` | Short-circuit the pipeline and speak `text` (skip the LLM) |
| `FastResult.miss()` | This matcher doesn't apply — fall through to the next matcher, then the LLM |
| `FastResult.clarify(text)` | Speak a clarifying question (skips the LLM) |

Matchers run in **descending `priority`** order; the first to return a handled/clarify result wins. A matcher that raises is logged and treated as a miss, so one bad skill can't break the pipeline.

**Design guidance:** keep fast intents *high-precision*. Match only what you're confident about and return `miss()` for anything ambiguous — the LLM is the safety net. Because the two front-ends are independent, a skill can be more forgiving in the LLM path while staying strict in the fast path.

A single skill file commonly exposes both: a `@fast_intent` for the easy cases and an `@skill` the LLM falls back on (see [`home_assistant.py`](home-assistant.md)). Both honour `skills.disabled` in `llm.yaml`.

For deterministic intent/slot parsing beyond simple keyword checks, the `llm` extra ships [`padacioso`](https://github.com/OpenVoiceOS/padacioso) (pure-Python, Padatious-style `.intent` syntax) and [`rapidfuzz`](https://github.com/rapidfuzz/RapidFuzz) (fuzzy name matching).

## Disabling a skill temporarily

Add the function name to `skills.disabled` in `llm.yaml`:

```yaml
skills:
  disabled:
    - my_skill
```

The file is not deleted; the skill is simply not registered at startup. This disables both its `@skill` and any `@fast_intent` of the same name.
