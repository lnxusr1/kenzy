"""The ``ask()`` primitive (4.2 "platform" headliner).

A skill that needs the user's answer mid-flow gets a clearly defined path
back — never re-dispatch roulette (an LLM or rules engine deciding whether
the next phrase returns to the calling skill)::

    reply = await ask("Should I create a list called Groceries?")
    if reply is None:          # wake word / timeout / restart — never a hot mic
        return FastResult.handled("Okay, never mind.")

Mechanics: the skill coroutine runs inside an :class:`asyncio.Task`. When it
awaits :func:`ask`, the prompt is handed to the request handler through a
per-invocation channel and the task PARKS on a future; the handler returns
the prompt to the server with a ``continuation`` id (the server speaks it and
arms the node's one-shot capture — the mic plumbing that already shipped for
dialogs/enrollment). The next captured utterance arrives on
``POST /process/continue``, resolves the future, and the coroutine resumes
exactly where it suspended — it may ask again, chaining turns.

Locked decisions (roadmap, founder):

- **The wake word always cancels** a pending ask — not skill-disableable;
  cancel resolves the future with ``None`` and the skill's return value is
  discarded (the node has already moved on).
- **Timeout defaults to the node's reply window** (``dialog_no_speech_timeout_ms``
  — enforced node-side; the server reports expiry and cancels). The
  registry's own deadline is only a backstop for a lost server, capped at
  :data:`MAX_PARK_S`.
- **The reply carries the ANSWERER's identity**: on resume, the parked
  request context is updated in place with the speaker who answered, so
  ``get_request("person_id")`` / ``current_tier()`` reflect who is actually
  talking — gated skills re-check.
- Continuations are **in-memory and mortal**: a kenzy-llm restart forgets
  them; the node's window expiry then cleans up the conversation side.

Context plumbing: the parked task runs in a COPY of the request context
(``asyncio.create_task`` semantics), so everything the handler must observe
from outside is a mutable object shared across the copy — the request dict
(mutated on resume with the answerer), the actions list, and the memory touch
markers (see ``memory.begin_touch``).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: Hard cap on how long a continuation may stay parked (lost-server backstop —
#: the normal lifecycle is server-driven: answered, wake-canceled, or timed out
#: at the node's reply window).
MAX_PARK_S = 600.0


class AskChannel:
    """The rendezvous between a running skill coroutine and its handler."""

    def __init__(self) -> None:
        self.prompt: str = ""
        self.timeout: float | None = None
        # "text" (default): the reply is the STT transcript. "audio": the reply
        # is the RAW captured PCM (16 kHz int16 bytes) — STT never runs; this is
        # what voice enrollment consumes. ``cue``: play the record-tone when the
        # window opens (record-after-the-tone flows) vs the silent dialog open.
        self.capture: str = "text"
        self.cue: bool = False
        self.asked = asyncio.Event()
        self.reply_fut: asyncio.Future[Any] | None = None
        # Shared mutable request state, captured at run() time so the handler
        # (and the resume path) can observe/update what the parked task sees.
        self.request_ctx: dict[str, Any] | None = None
        self.actions: list[dict[str, Any]] | None = None
        self.touch: dict[str, bool] | None = None  # memory touch markers (lockbox/private)


_CHANNEL: contextvars.ContextVar[AskChannel | None] = contextvars.ContextVar(
    "kenzy_ask_channel", default=None
)


async def ask(
    prompt: str,
    timeout: float | None = None,
    *,
    capture: str = "text",
    cue: bool = False,
) -> Any:
    """Speak ``prompt``, park until the user's answer arrives, return it.

    Returns ``None`` on cancel (wake word), node-window timeout, or service
    restart — the skill decides its fallback; there is never a silently hot
    mic. ``timeout`` (seconds) overrides the node's reply window for this one
    question, bounded by the runtime cap. ``capture="audio"`` returns the raw
    captured PCM bytes instead of a transcript (see :func:`ask_audio`).
    """
    ch = _CHANNEL.get()
    if ch is None:
        raise RuntimeError("ask() called outside an askable skill invocation")
    if ch.reply_fut is not None and not ch.reply_fut.done():
        raise RuntimeError("ask() re-entered while a question is already pending")
    loop = asyncio.get_running_loop()
    ch.prompt = str(prompt)
    ch.timeout = timeout
    ch.capture = capture
    ch.cue = cue
    ch.reply_fut = loop.create_future()
    ch.asked.set()
    try:
        return await ch.reply_fut
    finally:
        ch.reply_fut = None


async def ask_audio(prompt: str, timeout: float | None = None) -> bytes | None:
    """Speak ``prompt`` and return the user's RAW spoken reply as 16 kHz int16
    PCM bytes (no STT, no speaker-id — the audio itself is the answer; voice
    enrollment is the canonical consumer). Record-after-the-tone: the node
    plays its cue when the window opens. ``None`` on cancel/timeout/restart."""
    reply = await ask(prompt, timeout, capture="audio", cue=True)
    return bytes(reply) if isinstance(reply, (bytes, bytearray)) else None


@dataclass
class Parked:
    """A suspended skill invocation awaiting the user's answer."""

    id: str
    task: asyncio.Task[Any]
    channel: AskChannel
    kind: str  # "fast" | "llm" — which finisher the handler should apply
    meta: dict[str, Any] = field(default_factory=dict)
    created: float = field(default_factory=time.monotonic)

    @property
    def deadline(self) -> float:
        cap = min(self.channel.timeout or MAX_PARK_S, MAX_PARK_S)
        return self.created + cap


_PENDING: dict[str, Parked] = {}


class AskOutcome:
    """Result of driving an askable invocation one step: either the coroutine
    finished (``done`` holds its return value) or it parked on a question
    (``parked`` holds the registry entry; ``prompt``/``timeout`` mirror it)."""

    def __init__(self, *, done: Any = None, finished: bool, parked: Parked | None = None):
        self.finished = finished
        self.value = done
        self.parked = parked


async def run_askable(coro: Any, *, kind: str, meta: dict[str, Any] | None = None) -> AskOutcome:
    """Run a skill coroutine that MAY call ask(). Finishes normally for the
    overwhelming majority that never do; parks and registers a continuation
    for the ones that do."""
    from kenzy.llm import memory
    from kenzy.llm import skills as skill_registry

    ch = AskChannel()
    ch.request_ctx = skill_registry.current_request_dict()
    ch.actions = skill_registry.current_actions_list()
    ch.touch = memory.current_touch_dict()
    token = _CHANNEL.set(ch)
    try:
        task: asyncio.Task[Any] = asyncio.create_task(coro)
    finally:
        _CHANNEL.reset(token)
    return await _drive(task, ch, kind=kind, meta=meta or {})


async def _drive(
    task: asyncio.Task[Any], ch: AskChannel, *, kind: str, meta: dict[str, Any]
) -> AskOutcome:
    waiter = asyncio.create_task(ch.asked.wait())
    try:
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        if not waiter.done():
            waiter.cancel()
    if task.done():
        return AskOutcome(done=task.result(), finished=True)  # exceptions propagate
    parked = Parked(id=uuid.uuid4().hex[:12], task=task, channel=ch, kind=kind, meta=meta)
    _PENDING[parked.id] = parked
    _sweep()
    log.info("ask(): parked continuation %s (%s): %r", parked.id, kind, ch.prompt[:60])
    return AskOutcome(finished=False, parked=parked)


async def resume(cont_id: str, reply: Any, answerer: dict[str, Any] | None = None) -> AskOutcome:
    """Deliver the user's answer to a parked continuation and drive it to its
    next state (finished, or asking again). ``answerer`` updates the parked
    request context in place — the identity the resumed skill sees is who
    actually answered, not who originally asked."""
    parked = _PENDING.pop(cont_id, None)
    if parked is None:
        raise KeyError(cont_id)
    ch = parked.channel
    if answerer and ch.request_ctx is not None:
        ch.request_ctx.update(answerer)
    ch.asked.clear()
    assert ch.reply_fut is not None
    ch.reply_fut.set_result(reply)
    outcome = await _drive(parked.task, ch, kind=parked.kind, meta=parked.meta)
    if not outcome.finished and outcome.parked is not None:
        # Chained ask: carry the original meta forward under the NEW id.
        outcome.parked.meta = parked.meta
    return outcome


async def cancel(cont_id: str, reason: str = "canceled") -> None:
    """Wake word / node timeout / disconnect: resolve the pending question
    with None, let the coroutine run to completion, and DISCARD its result —
    the conversation has already moved on. Unknown ids are a no-op (the
    normal race with an answer that just arrived)."""
    parked = _PENDING.pop(cont_id, None)
    if parked is None:
        return
    ch = parked.channel
    ch.asked.clear()
    if ch.reply_fut is not None and not ch.reply_fut.done():
        ch.reply_fut.set_result(None)
    try:
        outcome = await _drive(parked.task, ch, kind=parked.kind, meta=parked.meta)
        if not outcome.finished and outcome.parked is not None:
            # The skill asked AGAIN after a None reply — that's a bug in the
            # skill, but never leave a task parked forever: cancel it too.
            _PENDING.pop(outcome.parked.id, None)
            outcome.parked.task.cancel()
            log.warning("ask(): %s re-asked after cancel — dropped", cont_id)
    except Exception as exc:
        log.warning("ask(): continuation %s errored during cancel: %s", cont_id, exc)
    log.info("ask(): continuation %s canceled (%s)", cont_id, reason)


def pending(cont_id: str) -> Parked | None:
    return _PENDING.get(cont_id)


def pending_count() -> int:
    return len(_PENDING)


def _sweep() -> None:
    """Drop continuations past their backstop deadline (lost-server case).
    Lazy — called on park; the future is resolved None so the task unwinds."""
    now = time.monotonic()
    for cid in [c for c, p in _PENDING.items() if now > p.deadline]:
        parked = _PENDING.pop(cid)
        ch = parked.channel
        if ch.reply_fut is not None and not ch.reply_fut.done():
            ch.reply_fut.set_result(None)
        log.info("ask(): continuation %s expired (backstop)", cid)
