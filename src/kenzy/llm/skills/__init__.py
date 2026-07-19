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
import types
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
# Request-scoped state (server-injected context)
# ---------------------------------------------------------------------------
# The server injects per-request context into /process (connected room names, the
# asking node's active schedule entries, …). Skills and fast intents read it via
# get_request() — the mirror of add_action(): actions flow out, context flows in.

_request_ctx: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("kenzy_request")


def begin_request(context: dict[str, Any]) -> None:
    """Set the per-request context (called by the LLM service before dispatch)."""
    _request_ctx.set(dict(context))


def get_request(key: str, default: Any = None) -> Any:
    """Read a server-injected request value (e.g. ``schedules``, ``rooms``)."""
    try:
        return _request_ctx.get().get(key, default)
    except LookupError:
        return default


def current_request_dict() -> dict[str, Any] | None:
    """The live request-context dict (shared object). The ask() runner captures
    it so a resume can update the parked task's view IN PLACE with the
    answerer's identity — contextvar .set() can't reach into a task's copied
    context, but mutating the shared dict can."""
    try:
        return _request_ctx.get()
    except LookupError:
        return None


def current_actions_list() -> list[dict[str, Any]] | None:
    """The live action accumulator (shared object) — same in-place rationale."""
    try:
        return _actions.get()
    except LookupError:
        return None


def request_channel() -> str:
    """Which front door this request came through: "voice" (a room node — the
    default, incl. legacy servers that don't send the field) or "assist"
    (HA Assist, F3 — no asking node exists). Node-bound skills use this to
    refuse gracefully instead of silently targeting a node that isn't there."""
    return str(get_request("channel", "voice") or "voice")


def is_node_bound_refused() -> str | None:
    """Refusal text for node-bound skills on nodeless channels, or None when
    a room node is asking and the skill may proceed."""
    if request_channel() == "voice":
        return None
    return "That needs a room speaker — ask me from one of the room devices instead."


# ---------------------------------------------------------------------------
# Identity tiers (F1.3) — the confidence contract skills consume.
# The strings are a WIRE CONTRACT with the server's identity resolver
# (kenzy.server.people): they ride ProcessRequest.speaker_tier into the
# request context. Deliberately duplicated here (not imported) so the LLM
# service never depends on server internals.
#
# The contract: UNKNOWN (no/low-confidence voice) gets device control and
# general Q&A only — no memory writes, no personal reads. RECOGNIZED (a
# voiceprint match) adds personalization/memory. VERIFIED (voiceprint
# corroborated by another signal) is reserved — a voiceprint alone is
# replayable, so anything that sends or spends terminates at a credentialed
# surface regardless of tier.
# ---------------------------------------------------------------------------

TIER_UNKNOWN = "unknown"
TIER_RECOGNIZED = "recognized"
TIER_VERIFIED = "verified"
_TIER_ORDER = {TIER_UNKNOWN: 0, TIER_RECOGNIZED: 1, TIER_VERIFIED: 2}


def current_tier() -> str:
    """The requesting speaker's confidence tier (defaults to unknown — outside
    a request, or when the server predates tiers, nothing gated is offered)."""
    tier = str(get_request("speaker_tier", TIER_UNKNOWN) or TIER_UNKNOWN)
    return tier if tier in _TIER_ORDER else TIER_UNKNOWN


def tier_allows(name: str) -> bool:
    """Whether the current request's tier meets ``name``'s declared min_tier."""
    need = _MIN_TIER.get(name)
    if need is None:
        return True
    return _TIER_ORDER[current_tier()] >= _TIER_ORDER[need]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, tuple[Callable[..., Any], dict[str, Any]]] = {}
_CONFIG: dict[str, Any] = {}

# name (skill or fast intent) → minimum tier required to use it (F1.3).
# Absent = available to everyone, including unrecognized voices.
_MIN_TIER: dict[str, str] = {}

# Deterministic fast-path matchers: (priority, name, async_fn). Higher priority
# runs first.  Kept sorted descending so dispatch is a simple in-order scan.
_FAST_REGISTRY: list[tuple[int, str, Callable[..., Any]]] = []

# Names disabled at runtime. Everything stays loaded in the registries above; the
# runtime paths (get_tools / execute / dispatch_fast) simply skip a disabled name.
# Keeping skills loaded-but-gated lets the dashboard toggle them live (no restart);
# the same set is seeded from skills.disabled at load.  A name disables both the
# @skill and any same-named @fast_intent.
_DISABLED: set[str] = set()
# name (skill or fast intent) → source module (file stem, e.g. "home_assistant").
# The module is the unit that MEANS a feature: skills.disabled accepts module
# names too, disabling every @skill and @fast_intent the file defines at once
# (the dashboard's group toggle, and what gates like is_disabled("home_assistant")
# query). Function-level entries still work for surgical disables.
_MODULES: dict[str, str] = {}


def _module_of(func: Any) -> str:
    return str(getattr(func, "__module__", "") or "").rsplit(".", 1)[-1]


def _inactive(name: str) -> bool:
    """True when a skill/fast-intent is off — by its own name or its module's."""
    return name in _DISABLED or _MODULES.get(name, "") in _DISABLED


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


# Optional local fallback model (llm.yaml `fallback:`) — tried once, silently,
# when the primary model call fails; if the fallback fails too, the exception
# propagates and the user just gets the spoken error cue.
_FALLBACK: dict[str, Any] = {}


def set_fallback(model: str | None, base_url: str | None) -> None:
    """Configure (or clear) the local fallback model. Called at service startup."""
    _FALLBACK.clear()
    if model:
        _FALLBACK["model"] = str(model)
        _FALLBACK["base_url"] = str(base_url) if base_url else None


def fallback_model() -> str:
    """The configured fallback model string (Activity span naming)."""
    return str(_FALLBACK.get("model") or "")


async def acompletion_with_fallback(
    kwargs: dict[str, Any], state: dict[str, Any] | None = None, *, local_only: bool = False
) -> Any:
    """LiteLLM call with a single silent retry on the configured local fallback.

    ``state`` is a per-request dict: once the primary has failed, the request is
    pinned to the fallback so a multi-iteration tool loop doesn't re-pay the
    primary's timeout on every turn. No fallback configured ⇒ behaves exactly
    like a plain ``acompletion`` call.

    ``local_only``: the caller's content must never reach a cloud model (the
    memory classifier / private-fact consolidation) — the fallback is used
    only when it is itself local; otherwise the primary's failure propagates.
    """
    from litellm import acompletion  # type: ignore[import-untyped]

    def _fallback_ok() -> bool:
        if not _FALLBACK.get("model"):
            return False
        if not local_only:
            return True
        from kenzy.llm.locality import model_is_local

        return model_is_local(str(_FALLBACK.get("model", "")), _FALLBACK.get("base_url"))

    if not (state and state.get("fallback")):
        try:
            return await acompletion(**kwargs)
        except Exception as exc:
            if not _fallback_ok():
                raise
            log.warning(
                "Primary model %r failed (%s) — falling back to %s",
                kwargs.get("model"),
                exc,
                _FALLBACK["model"],
            )
            if state is not None:
                state["fallback"] = True
    fb = dict(kwargs)
    fb.pop("base_url", None)
    fb.pop("api_key", None)
    fb["model"] = _FALLBACK["model"]
    fb.update(endpoint_kwargs(_FALLBACK.get("base_url")))
    return await acompletion(**fb)


def endpoint_kwargs(base_url: str | None) -> dict[str, Any]:
    """LiteLLM kwargs for an optional custom endpoint (Ollama / LM Studio / proxy).

    Security invariant (F-14 in the security design doc): ``OPENAI_API_KEY``
    **never travels to a custom base_url**. ``base_url`` is dashboard-editable by
    design, so a request to it must not inherit the highest-value secret in
    ``.env`` — otherwise repointing the URL exfiltrates the key via the
    Authorization header. With a base_url set, the credential is
    ``CUSTOM_LLM_API_KEY`` (hosted proxies that need auth), else a dummy —
    local servers ignore it. No base_url ⇒ no override: LiteLLM routes known
    providers to their official endpoints with their own env keys, as ever.
    """
    if not base_url:
        return {}
    import os

    return {
        "base_url": base_url,
        "api_key": os.environ.get("CUSTOM_LLM_API_KEY") or "sk-no-key-for-custom-endpoint",
    }


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

    # Optional[X] → treat as X (None handled by not being required). PEP 604
    # unions (`X | None`) have their own origin, types.UnionType — missing it
    # sent `list[str] | None` params to the string fallback, so the model was
    # TOLD to pass a string ("broccoli" → eight one-letter groceries).
    if origin is typing.Union or origin is types.UnionType:
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


def skill(
    _func: Callable[..., Any] | None = None, *, min_tier: str | None = None
) -> Callable[..., Any]:
    """Register an async function as a callable skill.

    ``min_tier`` (F1.3) declares the identity tier required to use it:
    ``"recognized"`` hides the tool from unrecognized voices entirely (it's
    not offered to the model, and a direct call is refused). Usable bare
    (``@skill``) or with the gate (``@skill(min_tier="recognized")``).
    """
    if min_tier is not None and min_tier not in _TIER_ORDER:
        raise ValueError(f"min_tier must be one of {sorted(_TIER_ORDER)}: {min_tier!r}")

    def wrap(func: Callable[..., Any]) -> Callable[..., Any]:
        if not asyncio.iscoroutinefunction(func):
            raise TypeError(f"@skill requires an async function: {func.__name__}")
        _REGISTRY[func.__name__] = (func, _generate_schema(func))
        _MODULES[func.__name__] = _module_of(func)
        if min_tier is not None:
            _MIN_TIER[func.__name__] = min_tier
        return func

    return wrap(_func) if _func is not None else wrap


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
    # Which matcher handled it — set by dispatch_fast for the Activity
    # breakdown (never set by the matcher itself).
    name: str = ""

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
    _func: Callable[..., Any] | None = None, *, priority: int = 0, min_tier: str | None = None
) -> Callable[..., Any]:
    """Register an async function as a deterministic fast-path matcher.

    The matcher is called as ``func(utterance, room_id, speaker)`` and must
    return a :class:`FastResult`.  Matchers run before the LLM in descending
    priority order; the first that returns a handled/clarify result
    short-circuits the pipeline.  Usable bare (``@fast_intent``) or with a
    priority (``@fast_intent(priority=100)``).

    ``min_tier`` (F1.3): below the declared tier the matcher is never even
    RUN (matchers may stage actions/state, so a skipped matcher must have no
    side effects) — the utterance falls through to the LLM, which won't hold
    the gated tool either and explains naturally.
    """
    if min_tier is not None and min_tier not in _TIER_ORDER:
        raise ValueError(f"min_tier must be one of {sorted(_TIER_ORDER)}: {min_tier!r}")

    def wrap(func: Callable[..., Any]) -> Callable[..., Any]:
        if not asyncio.iscoroutinefunction(func):
            raise TypeError(f"@fast_intent requires an async function: {func.__name__}")
        _FAST_REGISTRY.append((priority, func.__name__, func))
        _FAST_REGISTRY.sort(key=lambda t: t[0], reverse=True)
        _MODULES[func.__name__] = _module_of(func)
        if min_tier is not None:
            _MIN_TIER[func.__name__] = min_tier
        return func

    return wrap(_func) if _func is not None else wrap


async def ask(prompt: str, timeout: float | None = None) -> str | None:
    """Speak ``prompt`` and return the user's spoken answer (the 4.2 ask()
    primitive) — or None on wake-word cancel / timeout. Import from here in
    skill files: ``from kenzy.llm.skills import ask``. See kenzy.llm.asking."""
    from kenzy.llm import asking

    reply = await asking.ask(prompt, timeout)
    return reply if isinstance(reply, str) else None


async def ask_audio(prompt: str, timeout: float | None = None) -> bytes | None:
    """Speak ``prompt`` and return the user's raw spoken reply as 16 kHz PCM
    bytes (record-after-the-tone; no STT). None on cancel/timeout."""
    from kenzy.llm import asking

    return await asking.ask_audio(prompt, timeout)


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
        if _inactive(name):
            continue
        if not tier_allows(name):
            # Below the declared tier the matcher is never run (it may stage
            # actions or per-room state); the LLM tier handles the utterance.
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
            result.name = name
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
    fast_active = [t[1] for t in _FAST_REGISTRY if not _inactive(t[1])]
    if fast_active:
        log.info("Fast intents active: %s", fast_active)
    if _DISABLED:
        log.info("Skills disabled: %s", sorted(_DISABLED))


def _active_skill_names() -> list[str]:
    return [name for name in _REGISTRY if not _inactive(name)]


def set_disabled(names: list[str]) -> None:
    """Replace the runtime-disabled set (live, no reload). Idempotent."""
    global _DISABLED
    if isinstance(names, str):  # a bare string would iterate as characters
        names = [names]
    _DISABLED = {str(n) for n in names}
    _warn_unknown_disabled()


def _warn_unknown_disabled() -> None:
    """Log loudly when a skills.disabled entry matches nothing — a typo (or a
    name from a newer/older version) silently disabling nothing is a debugging
    trap (found the hard way: "home_assistant" was a no-op before modules)."""
    if not (_REGISTRY or _FAST_REGISTRY):
        return  # nothing loaded yet — validation happens after load
    known = set(_REGISTRY) | {t[1] for t in _FAST_REGISTRY} | set(_MODULES.values())
    unknown = _DISABLED - known
    if unknown:
        log.warning(
            "skills.disabled entries match no skill, fast intent, or module "
            "(check for typos): %s — known modules: %s",
            sorted(unknown),
            sorted(set(_MODULES.values())),
        )


def is_disabled(name: str) -> bool:
    """Whether a skill, fast intent, or whole MODULE is currently disabled.

    For a module name (e.g. "home_assistant"): true when the module itself is
    listed in skills.disabled, or when every @skill it defines has been
    individually disabled — so feature gates (lists' HA gate, the dashboard's
    HA-tab banner) reflect the operator's intent regardless of which toggles
    they used. For a function name: its own entry or its module's counts.
    """
    if name in _DISABLED or _MODULES.get(name, "") in _DISABLED:
        return True
    members = [n for n, m in _MODULES.items() if m == name and n in _REGISTRY]
    return bool(members) and all(n in _DISABLED for n in members)


def registry_info() -> dict[str, Any]:
    """Snapshot of loaded skills + fast intents for the dashboard registry view."""
    skills = []
    for name in sorted(_REGISTRY):
        _, schema = _REGISTRY[name]
        desc = schema.get("function", {}).get("description", "") or ""
        skills.append(
            {
                "name": name,
                "module": _MODULES.get(name, ""),
                "description": desc.strip().split("\n")[0][:200],
                "disabled": _inactive(name),
                "calls": _COUNTS.get(name, 0),
                "fast": any(t[1] == name for t in _FAST_REGISTRY),
                "min_tier": _MIN_TIER.get(name),
            }
        )
    fast = [
        {
            "name": name,
            "module": _MODULES.get(name, ""),
            "priority": priority,
            "disabled": _inactive(name),
            "calls": _COUNTS.get(name, 0),
            "skill": name in _REGISTRY,
            "min_tier": _MIN_TIER.get(name),
        }
        for priority, name, _ in _FAST_REGISTRY
    ]
    modules = sorted({m for m in _MODULES.values() if m})
    return {
        "skills": skills,
        "fast_intents": fast,
        # Module-level view for the dashboard's group toggles.
        "modules": [{"name": m, "disabled": is_disabled(m)} for m in modules],
    }


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


def get_tools() -> list[dict[str, Any]]:
    """Return the tool definitions to pass to LiteLLM — disabled skills excluded,
    and (F1.3) tools above the requesting speaker's tier withheld entirely, so
    the model can't be talked into calling what the speaker isn't entitled to."""
    return [
        schema
        for name, (_, schema) in _REGISTRY.items()
        if not _inactive(name) and tier_allows(name)
    ]


def _coerce_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    """Repair known model deviations from the tool schema before calling the skill.

    Models sometimes pass a bare string where the schema says array ("items":
    "broccoli") — without this, list-typed parameters iterate per character
    downstream (broccoli → 8 one-letter groceries)."""
    props = schema.get("function", {}).get("parameters", {}).get("properties", {})
    return {
        key: [val] if props.get(key, {}).get("type") == "array" and isinstance(val, str) else val
        for key, val in arguments.items()
    }


async def execute(name: str, arguments: dict[str, Any]) -> str:
    """Call a registered skill and return its string result."""
    if name not in _REGISTRY:
        return f"Unknown skill: {name!r}"
    if _inactive(name):  # not advertised in get_tools(), but guard anyway
        return f"Skill {name!r} is disabled."
    if not tier_allows(name):  # withheld from get_tools(), but guard anyway (F1.3)
        log.info(
            "Skill %r refused: requires tier %r, speaker is %r",
            name,
            _MIN_TIER[name],
            current_tier(),
        )
        return (
            f"Refused: {name!r} requires a {_MIN_TIER[name]} voice and the current "
            "speaker isn't recognized. Tell the user you can't do that for them until "
            "their voice is enrolled (a household member can enroll them from the "
            "dashboard's People tab)."
        )
    func, schema = _REGISTRY[name]
    try:
        result = await func(**_coerce_arguments(schema, arguments))
        _COUNTS[name] = _COUNTS.get(name, 0) + 1
        return str(result)
    except Exception as exc:
        log.warning("Skill %r raised: %s", name, exc)
        return f"The skill encountered an error: {exc}"
