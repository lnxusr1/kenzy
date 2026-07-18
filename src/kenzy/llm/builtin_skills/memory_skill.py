"""
Memory — remember / recall / forget / promote / demote (v4 F2.5).

The voice surface over the fact ledger (kenzy.llm.memory). Everything here is
gated ``min_tier="recognized"`` (F1.3): an unrecognized voice never sees these
tools and the fast intents never run for it. Facts are owned by the speaker's
**person id** (F1) — a recognized voiceprint that has no person record yet is
guided to the dashboard's People tab instead of writing unowned memory.

Tier semantics on the voice path (F2.3): writes default **private**; an
explicit share signal in the phrasing ("everyone should know…") or the promote
skill moves a fact to **shared**. ``personal-public`` exists in the ledger and
the wire contract, but v1 voice writes don't classify into it — that's the
F2.4 write-path classifier (phase 2).

Distinct from reminders: "remind me to take the bins out at 7" is the
scheduler; "remember that the bins go out on Tuesday" is memory. The fast
intent deliberately misses "remember to …" phrasings so the LLM can pick the
right tool.
"""

from __future__ import annotations

import re

from kenzy.llm import memory
from kenzy.llm.skills import FastResult, fast_intent, get_request, skill  # type: ignore[import]

_VOICE_PROMPT = "Speak naturally at a conversational pace."

_NO_STORE = "Memory is turned off on this system."
_NO_PERSON = (
    "I recognize your voice, but it isn't linked to a person yet — "
    "add yourself on the dashboard's People tab and I'll remember things for you."
)


def _asker() -> str | None:
    """The owner for every memory operation: the resolved person id, never a
    display name. None ⇒ no person record (min_tier already filtered unknown)."""
    pid = get_request("person_id")
    if get_request("memory_opt_out"):
        return None  # F7.4 "don't remember me": no ledger identity at all
    return str(pid) if pid else None


def _refusal_msg() -> str:
    """Why memory isn't available to this speaker — opted out vs. no record."""
    if get_request("memory_opt_out"):
        return (
            "Memory is turned off for you at your request — I'm not keeping "
            "or reading any facts about you."
        )
    return _NO_PERSON


def _fact_lines(facts: list[memory.Fact]) -> str:
    return "; ".join(f.text for f in facts)


# ---------------------------------------------------------------------------
# Skills (the LLM tier)
# ---------------------------------------------------------------------------


@skill(min_tier="recognized")
async def remember(fact: str, shared: bool = False) -> str:
    """Store a fact in long-term memory for the current speaker.

    Use when the user asks to remember/note/keep track of something factual
    ("remember that the gate code is 4312"). NOT for time-based reminders —
    use set_reminder for "remind me to…". Set shared=true only when the user
    explicitly wants the whole household to know ("everyone should know…",
    "remember for everyone…"); otherwise the fact stays private to them.
    """
    store = memory.store()
    if store is None:
        return _NO_STORE
    owner = _asker()
    if owner is None:
        # The spoken utterance carries the would-be secret even though we
        # refuse to store it — tag the history turn so the echo never replays
        # to anyone else in the room (same protection a successful private
        # write gets).
        memory.mark_private_touch()
        return _refusal_msg()
    tier = memory.TIER_SHARED if shared else memory.TIER_PRIVATE
    f = store.remember(owner, fact, tier=tier)
    memory.mark_if_sensitive([f])  # a private write's echo shouldn't replay to others
    return f"Remembered{' for everyone' if shared else ''}: {f.text}"


@skill(min_tier="recognized")
async def recall(topic: str) -> str:
    """Search the speaker's memory for facts about a topic.

    Use when the user asks what you know/remember about something ("what's
    the gate code?", "what do you remember about the plumber?"). Returns only
    what this speaker is allowed to see (their private facts + household
    facts).
    """
    store = memory.store()
    if store is None:
        return _NO_STORE
    owner = _asker()
    if owner is None:
        return _refusal_msg()
    facts = store.recall(owner, topic, limit=5)
    if not facts:
        return f"Nothing remembered about {topic}."
    memory.mark_if_sensitive(facts)
    return "Remembered facts: " + _fact_lines(facts)


@skill(min_tier="recognized")
async def forget(topic: str) -> str:
    """Erase remembered facts matching a topic, at the speaker's request.

    Use when the user asks to forget/delete/erase something they told you to
    remember. Only erases facts they own (or household-shared facts). If
    several facts match, they are listed so the user can narrow it down.
    """
    store = memory.store()
    if store is None:
        return _NO_STORE
    owner = _asker()
    if owner is None:
        return _refusal_msg()
    matches = [f for f in store.recall(owner, topic, limit=5) if f.erasable_by(owner)]
    if not matches:
        return f"Nothing remembered about {topic}."
    memory.mark_if_sensitive(matches)
    if len(matches) > 1:
        return "Several facts match — ask the user which one to forget: " + " | ".join(
            f.text for f in matches
        )
    store.forget(owner, matches[0].id)
    return f"Forgotten: {matches[0].text}"


@skill(min_tier="recognized")
async def share_memory(topic: str) -> str:
    """Promote a remembered fact so the whole household can see it.

    Use for "make that shared", "everyone should know that", "share that with
    the house" — about something already remembered. Matches the speaker's own
    facts by topic.
    """
    return await _retier(topic, memory.TIER_SHARED, "Shared with the household")


@skill(min_tier="recognized")
async def make_memory_private(topic: str) -> str:
    """Demote a remembered fact back to private ("keep that between us").

    Matches the speaker's own facts by topic and restricts them to the
    speaker only.
    """
    return await _retier(topic, memory.TIER_PRIVATE, "Kept private")


async def _retier(topic: str, tier: str, verb: str) -> str:
    store = memory.store()
    if store is None:
        return _NO_STORE
    owner = _asker()
    if owner is None:
        return _refusal_msg()
    matches = [f for f in store.recall(owner, topic, limit=5) if f.owner == owner]
    if not matches:
        return f"Nothing of yours remembered about {topic}."
    memory.mark_if_sensitive(matches)
    if len(matches) > 1:
        return "Several facts match — ask the user which one they mean: " + " | ".join(
            f.text for f in matches
        )
    store.set_tier(owner, matches[0].id, tier)
    return f"{verb}: {matches[0].text}"


# ---------------------------------------------------------------------------
# Fast intents — the high-frequency phrasings, no LLM
# ---------------------------------------------------------------------------

# "remember (that) X" / "don't forget (that) X"; a leading share signal makes it
# household-shared. "remember to …" is deliberately excluded (that's usually a
# reminder — let the LLM pick between set_reminder and remember).
_REMEMBER_RE = re.compile(
    r"^(?:please\s+)?(?:(?P<share>everyone should know|remember for everyone)|remember|don'?t forget)"
    r"\s+(?:that\s+)?(?P<fact>.+?)[.!?]?$",
    re.IGNORECASE,
)
_RECALL_RE = re.compile(
    r"^(?:please\s+)?what do you (?:know|remember) about\s+(?P<topic>.+?)[?.!]?$",
    re.IGNORECASE,
)
_FORGET_RE = re.compile(
    r"^(?:please\s+)?forget (?:about\s+)?(?P<topic>.+?)[.!?]?$",
    re.IGNORECASE,
)


@fast_intent(priority=92, min_tier="recognized")
async def fast_memory(utterance: str, room_id: str | None, speaker: str | None) -> FastResult:
    store = memory.store()
    if store is None:
        return FastResult.miss()
    owner = _asker()
    text = utterance.strip()

    m = _REMEMBER_RE.match(text)
    if m:
        fact = m.group("fact").strip()
        if fact.lower().startswith("to "):
            return FastResult.miss()  # "remember to …" — probably a reminder; LLM decides
        if owner is None:
            memory.mark_private_touch()  # the utterance carries the secret — no echo
            return FastResult.handled(_refusal_msg(), _VOICE_PROMPT)
        shared = bool(m.group("share"))
        stored = store.remember(
            owner, fact, tier=memory.TIER_SHARED if shared else memory.TIER_PRIVATE
        )
        return FastResult.handled(
            f"Okay, I'll remember that{' — and everyone can ask me' if shared else ''}.",
            _VOICE_PROMPT,
        )

    m = _RECALL_RE.match(text)
    if m:
        if owner is None:
            return FastResult.handled(_refusal_msg(), _VOICE_PROMPT)
        facts = store.recall(owner, m.group("topic"), limit=3)
        if not facts:
            return FastResult.miss()  # nothing remembered — the LLM may still know
        memory.mark_if_sensitive(facts)
        return FastResult.handled(_fact_lines(facts) + ".", _VOICE_PROMPT)

    m = _FORGET_RE.match(text)
    if m:
        topic = m.group("topic").strip()
        # Bare "forget it/that" is a colloquial bail-out, not an erase request.
        if topic.lower() in ("it", "that", "this", "everything"):
            return FastResult.miss()
        if owner is None:
            return FastResult.handled(_refusal_msg(), _VOICE_PROMPT)
        matches = [f for f in store.recall(owner, topic, limit=5) if f.erasable_by(owner)]
        if len(matches) != 1:
            return FastResult.miss()  # none or ambiguous — the LLM tool can clarify
        memory.mark_if_sensitive(matches)
        store.forget(owner, matches[0].id)
        return FastResult.handled(f"Forgotten: {matches[0].text}", _VOICE_PROMPT)

    return FastResult.miss()
