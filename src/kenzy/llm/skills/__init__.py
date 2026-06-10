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
import importlib.util
import inspect
import json
import logging
import sys
import typing
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, tuple[Callable[..., Any], dict[str, Any]]] = {}
_CONFIG: dict[str, Any] = {}


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
    if annotation is str:                return {"type": "string"}
    if annotation is int:                return {"type": "integer"}
    if annotation is float:              return {"type": "number"}
    if annotation is bool:               return {"type": "boolean"}

    origin = typing.get_origin(annotation)
    args   = typing.get_args(annotation)

    if origin is typing.Literal:
        return {"type": "string", "enum": list(args)}

    if origin is list:
        return {"type": "array", "items": _py_to_json_type(args[0]) if args else {}}

    if origin is typing.Union:
        # Optional[X] → treat as X (None handled by not being required)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _py_to_json_type(non_none[0])

    return {"type": "string"}   # safe fallback


def _generate_schema(func: Callable[..., Any]) -> dict[str, Any]:
    """Build a LiteLLM-compatible tool definition from a function."""
    sig   = inspect.signature(func)
    hints = typing.get_type_hints(func)
    doc   = inspect.getdoc(func) or func.__name__

    properties: dict[str, Any] = {}
    required:   list[str]      = []

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
            "name":        func.__name__,
            "description": doc,
            "parameters": {
                "type":       "object",
                "properties": properties,
                "required":   required,
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
# Discovery
# ---------------------------------------------------------------------------

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


def load_skills(skills_dir: Path, disabled: list[str]) -> None:
    """Load all skills from skills_dir, then remove any that are disabled."""
    if skills_dir.is_dir():
        _load_dir(skills_dir)
    else:
        log.warning("skills.dir does not exist: %s — no skills loaded", skills_dir)

    for name in disabled:
        if _REGISTRY.pop(name, None) is not None:
            log.info("Skill disabled: %s", name)

    log.info("Skills active: %s", sorted(_REGISTRY))


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

def get_tools() -> list[dict[str, Any]]:
    """Return the tool definitions to pass to LiteLLM."""
    return [schema for _, schema in _REGISTRY.values()]


async def execute(name: str, arguments: dict[str, Any]) -> str:
    """Call a registered skill and return its string result."""
    if name not in _REGISTRY:
        return f"Unknown skill: {name!r}"
    func, _ = _REGISTRY[name]
    try:
        result = await func(**arguments)
        return str(result)
    except Exception as exc:
        log.warning("Skill %r raised: %s", name, exc)
        return f"The skill encountered an error: {exc}"
