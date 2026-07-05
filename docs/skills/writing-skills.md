# Writing Skills

A skill is an async Python function decorated with `@skill`. Drop the file in your skills overlay directory — `~/.config/kenzy/skills/` (or `skills/` in a dev checkout), set by `skills.dir` in `llm.yaml` — and it is loaded automatically at startup, alongside the built-in skills. A file that defines a skill of the same name as a built-in overrides it.

!!! tip "Start from the complete example"
    [`examples/skills/example_skill.py`](https://github.com/lnxusr1/kenzy/blob/main/examples/skills/example_skill.py)
    is one runnable file demonstrating everything on this page — an `@skill`, a
    `@fast_intent`, per-skill config, request context, and a server action —
    with try-it instructions in its header. It's loaded by the test suite through
    the real overlay loader, so it's guaranteed current. Copy it into your
    overlay directory and say *"give me a fortune."*

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

    location: city and state or full address, e.g. "New York, NY"
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
from kenzy.llm.skills import endpoint_kwargs, get_config

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
    kwargs.update(endpoint_kwargs(base_url))

    response = await acompletion(**kwargs)
    return response.choices[0].message.content or ""
```

!!! warning "Always use `endpoint_kwargs` for a custom `base_url`"
    A `base_url` redirects the model call to a server of your choosing (a local
    Ollama or LM Studio, or a hosted proxy). Passed to LiteLLM directly, the
    request would still carry the configured cloud provider's API key from the
    environment — meaning whatever server the URL points at receives a real
    credential, and a mistyped or tampered URL becomes a key leak.
    `endpoint_kwargs(base_url)` applies Kenzy's rule instead: a custom endpoint
    only ever receives `CUSTOM_LLM_API_KEY` (set it if your endpoint requires
    auth) or a harmless placeholder — cloud provider keys stay bound to their
    own providers.

## Server-side actions

A skill runs inside `kenzy-llm`, which holds no node connections — it can't speak in
another room, ring an intercom, or schedule a timer by itself. For those, queue an
**action** the server actuates after the reply is spoken:

```python
from kenzy.llm.skills import add_action, skill

@skill
async def doorbell_test(room: str) -> str:
    """Play a spoken test announcement in a named room. ..."""
    add_action({"type": "announce", "text": "This is a test.", "rooms": [room]})
    return f"Queued a test announcement for {room}."
```

Actions ship with the response and are dispatched by the server. The built-in action
types: `announce`, `start_intercom`, `set_volume`, `start_enrollment`,
`set_schedule`, `cancel_schedule` — see the built-ins that use them
(`announce.py`, `volume.py`, `schedule.py`) for the exact payloads. Custom action
types require a matching handler in the server, so user skills should stick to the
built-in ones.

## Server-injected request context

The server injects per-request context that skills and fast intents can read with
`get_request(key, default)`:

| Key | Value |
|---|---|
| `room_id` | The asking room's name |
| `rooms` | Names of all currently connected rooms (validate targets against this) |
| `schedules` | The asking node's active timers/alarms/reminders (with ids) |

```python
from kenzy.llm.skills import get_request

rooms = get_request("rooms") or []
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
| `FastResult.clarify(text)` | Speak a clarifying question and **re-open the mic** for the answer (no wake word needed) |

Matchers run in **descending `priority`** order; the first to return a handled/clarify result wins. A matcher that raises is logged and treated as a miss, so one bad skill can't break the pipeline.

**Design guidance:** keep fast intents *high-precision*. Match only what you're confident about and return `miss()` for anything ambiguous — the LLM is the safety net. Because the two front-ends are independent, a skill can be more forgiving in the LLM path while staying strict in the fast path.

A single skill file commonly exposes both: a `@fast_intent` for the easy cases and an `@skill` the LLM falls back on (see [`home_assistant.py`](home-assistant.md)). Both honour `skills.disabled` in `llm.yaml`.

For deterministic intent/slot parsing beyond simple keyword checks, the `llm` extra ships [`padacioso`](https://github.com/OpenVoiceOS/padacioso) (pure-Python, Padatious-style `.intent` syntax) and [`rapidfuzz`](https://github.com/rapidfuzz/RapidFuzz) (fuzzy name matching).

## The development loop

1. Save your file under `~/.config/kenzy/skills/`.
2. Restart the LLM service to load it: `systemctl --user restart kenzy-llm`
   (or the **Restart** button on the dashboard's Services → llm page).
3. Verify it registered: the dashboard's **Skills** tab lists every loaded skill and
   fast intent, with invocation counts — if yours is missing, the file failed to
   import (check Logs → llm for the traceback).
4. Talk to it. The Activity tab shows what was heard, whether the fast path or the
   LLM answered, and the response.

## Disabling a skill temporarily

Toggle it off in the dashboard's **Skills** tab — applied live, no restart, and
persisted. The tab groups skills by their source **module** (file), with a
"Disable all" toggle per group — that's how you turn off a whole feature like
Home Assistant, which is really several skills (`handle_home_control`,
`get_device_states`, the list tools) plus their fast intents.

In `llm.yaml`, `skills.disabled` accepts either level:

```yaml
skills:
  disabled:
    - my_skill        # one function
    - home_assistant  # a whole module: every @skill AND @fast_intent in the file
```

A disabled skill stays loaded but is gated out of the tool list and the fast
path. Disabling a module silences its fast intents too; disabling a lone
function silences only that function.
