"""
Skill registry for kenzy-llm.

Skills are plain async Python functions decorated with @skill.  The
decorator introspects the function's signature, type hints, and docstring
to generate the JSON Schema tool definition that LiteLLM passes to the LLM.
No separate schema files or config entries are needed.

Adding a skill
--------------
1. Create a .py file in the skills/ directory.
2. Write an async function with type-annotated parameters and a clear
   docstring (the LLM reads the docstring to decide when to call the skill).
3. Decorate it with @skill.
4. It is enabled automatically unless listed under skills.disabled in llm.yaml.

Skill config / secrets
----------------------
Skills read API keys from environment variables (.env).  Other settings
(default location, service URLs, etc.) are available via get_config().
"""

from __future__ import annotations

import asyncio
import contextvars
import importlib.util
import inspect
import logging
import sys
import typing
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Server-side actions (request-scoped)
# ---------------------------------------------------------------------------
# Some skills don't compute an answer — they ask the *server* to do something the
# LLM service can't (it doesn't hold the node connections), e.g. broadcast an
# announcement or start an intercom call. Such a skill records an action here; the
# LLM service collects them after the tool loop and returns them on ProcessResponse
# for the server to actuate. A ContextVar keeps this isolated per /process request.

_actions: contextvars.ContextVar[list[dict[str, Any]]] = contextvars.ContextVar("kenzy_actions")


def begin_actions() -> contextvars.Token[list[dict[str, Any]]]:
    """Start a fresh action accumulator for the current request; returns a reset token."""
    return _actions.set([])


def add_action(action: dict[str, Any]) -> None:
    """Queue a server-side action (e.g. ``{"type": "announce", ...}``) from a skill."""
    try:
        _actions.get().append(action)
    except LookupError:  # called outside a request scope — no-op
        log.debug("add_action(%s) outside a request scope — ignored", action.get("type"))


def take_actions() -> list[dict[str, Any]]:
    """Return the actions queued during this request (empty if none / no scope)."""
    try:
        return list(_actions.get())
    except LookupError:
        return []


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, tuple[Callable[..., Any], dict[str, Any]]] = {}
_CONFIG: dict[str, Any] = {}

# Deterministic fast-path matchers: (priority, name, async_fn). Higher priority
# runs first.  Kept sorted descending so dispatch is a simple in-order scan.
_FAST_REGISTRY: list[tuple[int, str, Callable[..., Any]]] = []

# Names disabled at runtime. Everything stays loaded in the registries above; the
# runtime paths (get_tools / execute / dispatch_fast) simply skip a disabled name.
# Keeping skills loaded-but-gated lets the dashboard toggle them live (no restart);
# the same set is seeded from skills.disabled at load.  A name disables both the
# @skill and any same-named @fast_intent.
_DISABLED: set[str] = set()

# Per-name invocation counts (skill executes + fast-intent handles), for the
# dashboard's skill-registry view.  Best-effort, in-memory, reset on restart.
_COUNTS: dict[str, int] = {}


def set_config(cfg: dict[str, Any]) -> None:
    """Called at startup with the skills section of llm.yaml."""
    global _CONFIG
    _CONFIG = cfg


def get_config(section: str, key: str, default: Any = None) -> Any:
    """Retrieve a skill-specific config value from llm.yaml."""
    return _CONFIG.get(section, {}).get(key, default)


# ---------------------------------------------------------------------------
# Type → JSON Schema
# ---------------------------------------------------------------------------


def _py_to_json_type(annotation: Any) -> dict[str, Any]:
    """Convert a Python type annotation to a JSON Schema fragment."""
    if annotation is str:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}

    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    if origin is typing.Literal:
        return {"type": "string", "enum": list(args)}

    if origin is list:
        return {"type": "array", "items": _py_to_json_type(args[0]) if args else {}}

    if origin is typing.Union:
        # Optional[X] → treat as X (None handled by not being required)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _py_to_json_type(non_none[0])

    return {"type": "string"}  # safe fallback


def _generate_schema(func: Callable[..., Any]) -> dict[str, Any]:
    """Build a LiteLLM-compatible tool definition from a function."""
    sig = inspect.signature(func)
    hints = typing.get_type_hints(func)
    doc = inspect.getdoc(func) or func.__name__

    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        hint = hints.get(name, str)
        properties[name] = _py_to_json_type(hint)
        if param.default is inspect.Parameter.empty:
            required.append(name)

    return {
        "type": "function",
        "function": {
            "name": func.__name__,
            "description": doc,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


# ---------------------------------------------------------------------------
# @skill decorator
# ---------------------------------------------------------------------------


def skill(func: Callable[..., Any]) -> Callable[..., Any]:
    """Register an async function as a callable skill."""
    if not asyncio.iscoroutinefunction(func):
        raise TypeError(f"@skill requires an async function: {func.__name__}")
    _REGISTRY[func.__name__] = (func, _generate_schema(func))
    return func


# ---------------------------------------------------------------------------
# Fast-path (deterministic) intents
# ---------------------------------------------------------------------------


@dataclass
class FastResult:
    """Outcome of a deterministic fast-intent matcher.

    Construct via the classmethods:
      handled  – short-circuit the pipeline and speak ``text`` (skip the LLM).
      miss     – this matcher does not apply; defer to the next matcher / LLM.
      clarify  – speak a clarifying question; still skips the LLM and (once the
                 server honours it) re-opens the mic for the user's reply.
    """

    status: str  # "handled" | "miss" | "clarify"
    text: str = ""
    voice_prompt: str | None = None  # None → caller substitutes its default
    expect_response: bool = False

    @classmethod
    def handled(
        cls, text: str, voice_prompt: str | None = None, expect_response: bool = False
    ) -> FastResult:
        return cls("handled", text, voice_prompt, expect_response)

    @classmethod
    def miss(cls) -> FastResult:
        return cls("miss")

    @classmethod
    def clarify(cls, text: str, voice_prompt: str | None = None) -> FastResult:
        return cls("clarify", text, voice_prompt, expect_response=True)

    @property
    def is_handled(self) -> bool:
        return self.status in ("handled", "clarify")


def fast_intent(
    _func: Callable[..., Any] | None = None, *, priority: int = 0
) -> Callable[..., Any]:
    """Register an async function as a deterministic fast-path matcher.

    The matcher is called as ``func(utterance, room_id, speaker)`` and must
    return a :class:`FastResult`.  Matchers run before the LLM in descending
    priority order; the first that returns a handled/clarify result
    short-circuits the pipeline.  Usable bare (``@fast_intent``) or with a
    priority (``@fast_intent(priority=100)``).
    """

    def wrap(func: Callable[..., Any]) -> Callable[..., Any]:
        if not asyncio.iscoroutinefunction(func):
            raise TypeError(f"@fast_intent requires an async function: {func.__name__}")
        _FAST_REGISTRY.append((priority, func.__name__, func))
        _FAST_REGISTRY.sort(key=lambda t: t[0], reverse=True)
        return func

    return wrap(_func) if _func is not None else wrap


async def dispatch_fast(
    utterance: str, room_id: str | None, speaker: str | None
) -> FastResult | None:
    """Run deterministic matchers in priority order.

    Returns the first handled/clarify result, or ``None`` if every matcher
    misses (the caller should then fall through to the LLM).  A matcher that
    raises is logged and treated as a miss so one bad skill can't break the
    pipeline.
    """
    for _priority, name, func in _FAST_REGISTRY:
        if name in _DISABLED:
            continue
        try:
            result: FastResult | None = await func(utterance, room_id, speaker)
        except Exception as exc:
            log.warning("Fast intent %r raised: %s", name, exc)
            continue
        if result is None or result.status == "miss":
            continue
        if result.is_handled:
            log.info("Fast intent %r handled: %s", name, result.text[:80])
            _COUNTS[name] = _COUNTS.get(name, 0) + 1
            return result
    return None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

#: Built-in skills bundled inside the package (loaded before any user overlay).
_BUILTIN_DIR = Path(__file__).resolve().parent.parent / "builtin_skills"


def _load_dir(directory: Path) -> None:
    """Import all non-private .py files from a directory into the registry."""
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            # Register in sys.modules BEFORE exec so that typing.get_type_hints()
            # can resolve annotations (needed for @dataclass and from __future__
            # import annotations in skill modules).
            sys.modules[path.stem] = mod
            try:
                spec.loader.exec_module(mod)  # type: ignore[union-attr]
                log.debug("Loaded skills from %s", path)
            except Exception as exc:
                del sys.modules[path.stem]
                log.warning("Failed to load skill module %s: %s", path.name, exc)


def _dedupe_fast_registry() -> None:
    """Keep only the last registration for each fast-intent name, then re-sort.

    A user overlay file can re-register a fast intent of the same name as a
    built-in; since built-ins load first, the later (user) entry wins.
    """
    seen: dict[str, tuple[int, str, Callable[..., Any]]] = {}
    for entry in _FAST_REGISTRY:
        seen[entry[1]] = entry
    _FAST_REGISTRY[:] = sorted(seen.values(), key=lambda t: t[0], reverse=True)


def load_skills(user_dir: Path | None, disabled: list[str]) -> None:
    """Load built-in skills, then user skills from ``user_dir``, then apply disables.

    Built-ins (bundled in the package) load first; ``user_dir`` (the config-home
    ``skills/`` overlay) loads second so a user file overrides a built-in of the
    same name. ``disabled`` names are then removed from both registries.
    """
    if _BUILTIN_DIR.is_dir():
        _load_dir(_BUILTIN_DIR)
    else:  # pragma: no cover - only if the package is broken
        log.warning("Built-in skills directory missing: %s", _BUILTIN_DIR)

    if user_dir is not None and user_dir.is_dir():
        _load_dir(user_dir)
    elif user_dir is not None:
        log.debug("User skills.dir does not exist: %s — built-ins only", user_dir)

    _dedupe_fast_registry()

    # Everything stays loaded; disabling is a runtime gate so the dashboard can
    # toggle it live. Seed the gate from skills.disabled (names that don't match
    # anything are kept anyway — harmless, and tolerant of typos / future skills).
    set_disabled(disabled)

    log.info("Skills active: %s", sorted(_active_skill_names()))
    fast_active = [t[1] for t in _FAST_REGISTRY if t[1] not in _DISABLED]
    if fast_active:
        log.info("Fast intents active: %s", fast_active)
    if _DISABLED:
        log.info("Skills disabled: %s", sorted(_DISABLED))


def _active_skill_names() -> list[str]:
    return [name for name in _REGISTRY if name not in _DISABLED]


def set_disabled(names: list[str]) -> None:
    """Replace the runtime-disabled set (live, no reload). Idempotent."""
    global _DISABLED
    _DISABLED = {str(n) for n in names}


def registry_info() -> dict[str, Any]:
    """Snapshot of loaded skills + fast intents for the dashboard registry view."""
    skills = []
    for name in sorted(_REGISTRY):
        _, schema = _REGISTRY[name]
        desc = schema.get("function", {}).get("description", "") or ""
        skills.append(
            {
                "name": name,
                "description": desc.strip().split("\n")[0][:200],
                "disabled": name in _DISABLED,
                "calls": _COUNTS.get(name, 0),
                "fast": any(t[1] == name for t in _FAST_REGISTRY),
            }
        )
    fast = [
        {
            "name": name,
            "priority": priority,
            "disabled": name in _DISABLED,
            "calls": _COUNTS.get(name, 0),
            "skill": name in _REGISTRY,
        }
        for priority, name, _ in _FAST_REGISTRY
    ]
    return {"skills": skills, "fast_intents": fast}


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


def get_tools() -> list[dict[str, Any]]:
    """Return the tool definitions to pass to LiteLLM (disabled skills excluded)."""
    return [schema for name, (_, schema) in _REGISTRY.items() if name not in _DISABLED]


async def execute(name: str, arguments: dict[str, Any]) -> str:
    """Call a registered skill and return its string result."""
    if name not in _REGISTRY:
        return f"Unknown skill: {name!r}"
    if name in _DISABLED:  # not advertised in get_tools(), but guard anyway
        return f"Skill {name!r} is disabled."
    func, _ = _REGISTRY[name]
    try:
        result = await func(**arguments)
        _COUNTS[name] = _COUNTS.get(name, 0) + 1
        return str(result)
    except Exception as exc:
        log.warning("Skill %r raised: %s", name, exc)
        return f"The skill encountered an error: {exc}"
