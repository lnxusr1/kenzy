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
import time

from kenzy.llm import lockbox, memory
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
async def offer_to_remember(fact: str) -> str:
    """Offer to remember a durable personal fact the user just mentioned —
    asks THEM aloud first and stores only on their spoken yes (the "suggest"
    capture mode). Use only when the speaker's capture mode suggests it;
    report the outcome briefly.

    :param fact: The fact, phrased from the speaker's view ("your dentist is
        Dr. Marsh").
    """
    from kenzy.llm.skills import ask, request_channel

    owner = _asker()
    if owner is None:
        return _refusal_msg()
    if get_request("memory_opt_out"):
        return "Memory is turned off for this speaker at their request."
    if request_channel() != "voice":
        return ""  # the suggest flow needs a held mic; stay silent elsewhere
    store = memory.store()
    if store is None:
        return "Memory isn't enabled on this system."
    answer = await ask(f"Want me to remember that {fact.strip().rstrip('.')}?")
    if answer is None:
        return "Okay."  # canceled — discarded upstream
    if _normalize(answer) in _YES_WORDS:
        store.remember(owner, fact.strip(), source="suggested", state="quarantined")
        return "Remembered."
    return "Okay, I won't remember it."


def _normalize(text: str) -> str:
    return re.sub(r"[^\w\s]", "", text or "").strip().lower()


_YES_WORDS = frozenset(
    {"yes", "yeah", "yep", "yup", "sure", "okay", "ok", "please", "please do",
     "go ahead", "do it", "yes please", "sounds good"}  # fmt: skip
)


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
    # Provenance: in auto-capture mode, LLM-tier writes are the model's
    # initiative (explicit phrasings land on the fast path) — label them so
    # the dashboard shows which memories she chose vs. which were dictated.
    src = "auto" if get_request("memory_capture") == "auto" else "voice"
    f = store.remember(owner, fact, tier=tier, state="quarantined", source=src)
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
    facts = [f for f in store.recall(owner, topic, limit=5) if f.state != "quarantined"]
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
    matches = [
        f
        for f in store.recall(owner, topic, limit=5)
        if f.erasable_by(owner) and f.state != "quarantined"
    ]
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
    matches = [
        f
        for f in store.recall(owner, topic, limit=5)
        if f.owner == owner and f.state != "quarantined"
    ]
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
# Explicit secret signal → the lockbox, synchronously (no classifier, and on
# the fast path no model ever sees the utterance), e.g. "remember this
# secretly: the gate code is 4312". A bare "secret" must be followed by
# punctuation ("remember this secret: …") so content that merely STARTS with
# the word — "remember that secret santa is on friday" — stays ordinary
# memory (field finding from review). A period counts as that punctuation:
# STT renders "keep this secret: my code is X" as two sentences ("…secret. My
# code is X.") often enough that the colon form alone missed on the rig —
# the same finding the suffix form below already carries.
# [,\s]+ between the verb and the signal word: Whisper freely decorates spoken
# pauses with commas ("Remember, secretly, my…"), and a comma must not knock
# the exchange off the deterministic path onto a model that would see the value.
_SECRET_RE = re.compile(
    r"^(?:please[,\s]+)?(?:remember|keep|store|lock)[,\s]+(?:this[,\s]+|that[,\s]+)?"
    r"(?:secretly|in the lockbox|(?:as a )?secret(?=\s*[:,.]))\s*[:,.]?\s*(?P<fact>.+?)[.!?]?$",
    re.IGNORECASE,
)
_SECRET_SUFFIX_RE = re.compile(
    # The separator tolerates sentence punctuation: STT often renders the
    # trailing clause as its own sentence ("…is 5150. Keep it secret.").
    r"^(?:please\s+)?remember\s+(?:that\s+)?(?P<fact>.+?)\s*[-—,.;]?\s*"
    r"(?:but\s+)?(?:keep (?:it|that) (?:a\s+)?secret|secretly|between us)[.!?]?$",
    re.IGNORECASE,
)
_REMEMBER_RE = re.compile(
    r"^(?:please\s+)?(?:(?P<share>everyone should know|remember for everyone)|remember|don'?t forget)"
    r"\s+(?:that\s+)?(?P<fact>.+?)[.!?]?$",
    re.IGNORECASE,
)
# "what do you know about X" and the natural shortenings people (and STT,
# which loves to drop a leading "What") actually produce: "do you know about
# X", "tell me what you know about X", "what do you remember about X".
_RECALL_RE = re.compile(
    r"^(?:please\s+)?(?:tell me\s+)?(?:what\s+)?do you (?:know|remember) about\s+"
    r"(?P<topic>.+?)[?.!]?$",
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

    m = _SECRET_RE.match(text) or _SECRET_SUFFIX_RE.match(text)
    if m:
        memory.mark_private_touch()  # the utterance IS the secret — never echo
        memory.mark_lockbox_touch()  # ...and never stored into history/short-term
        box = lockbox.store()
        if box is None:
            return FastResult.handled(
                "The lockbox isn't available on this system, so I didn't store that.",
                _VOICE_PROMPT,
            )
        if owner is None:
            return FastResult.handled(_refusal_msg(), _VOICE_PROMPT)
        box.add(owner, m.group("fact").strip(), source="voice")
        return FastResult.handled(
            "Locked away — only you can ask me for it.", _VOICE_PROMPT
        )

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
            owner,
            fact,
            tier=memory.TIER_SHARED if shared else memory.TIER_PRIVATE,
            state="quarantined",
        )
        return FastResult.handled(
            f"Okay, I'll remember that{' — and everyone can ask me' if shared else ''}.",
            _VOICE_PROMPT,
        )

    m = _RECALL_RE.match(text)
    if m:
        if owner is None:
            return FastResult.handled(_refusal_msg(), _VOICE_PROMPT)
        topic = m.group("topic").strip().lower()
        # F2.6 inspection: "about me" / "about the house" get honest summaries
        # instead of a keyword miss.
        if topic in ("me", "about me", "myself"):
            mine = [f for f in store.export(owner) if f.live(time.time())]
            box0 = lockbox.store()
            nsec = len(box0.list_for(owner)) if box0 else 0
            if not mine and not nsec:
                return FastResult.handled(
                    "Nothing yet — say 'remember that…' and I'll keep it for you.",
                    _VOICE_PROMPT,
                )
            memory.mark_if_sensitive(mine)
            parts = [f"I hold {len(mine)} memor{'y' if len(mine) == 1 else 'ies'} for you"]
            if nsec:
                parts.append(f"and {nsec} lockbox secret{'s' if nsec != 1 else ''}")
            recent = "; ".join(f.text for f in mine[-3:])
            tail = f" Most recent: {recent}." if recent else ""
            return FastResult.handled(" ".join(parts) + "." + tail, _VOICE_PROMPT)
        if topic in ("the house", "the household", "everyone", "us"):
            shared = [
                f
                for f in store.all_facts()
                if f.tier == memory.TIER_SHARED
                and f.state != "quarantined"
                and f.live(time.time())
            ]
            if not shared:
                return FastResult.handled(
                    "No household memories yet — say 'everyone should know…' to add one.",
                    _VOICE_PROMPT,
                )
            recent = "; ".join(f.text for f in shared[:3])
            return FastResult.handled(
                f"The house shares {len(shared)} memor{'y' if len(shared) == 1 else 'ies'}. "
                f"Most recent: {recent}.",
                _VOICE_PROMPT,
            )
        box = lockbox.store()
        if box is not None:
            # require_all: every content-token of the topic must appear in the
            # secret — "gym hours" must not read back the gym locker code
            # (review finding: any-overlap spoke secrets on a one-word graze).
            secrets = box.find(owner, m.group("topic"), require_all=True)
            if secrets:
                # Verbatim, owner-only, and NEVER via the LLM: the whole point.
                memory.mark_private_touch()
                memory.mark_lockbox_touch()  # reply carries the value — keep it out of buffers
                if not get_request("tts_local"):
                    # Founder decision 2026-07-18: a secret is never spoken
                    # through a cloud TTS provider — deflect to the dashboard.
                    return FastResult.handled(lockbox.DEFLECT_TEXT, _VOICE_PROMPT)
                return FastResult.handled(secrets[-1].text + ".", _VOICE_PROMPT)
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
        box = lockbox.store()
        if box is not None:
            # require_all here too: "forget the gym schedule" must never
            # silently delete the gym locker secret on a one-word overlap.
            hits = box.find(owner, topic, require_all=True)
            if len(hits) == 1:
                box.erase(owner, hits[0].id)
                memory.mark_private_touch()
                return FastResult.handled("Forgotten — it's out of the lockbox.", _VOICE_PROMPT)
        matches = [
        f
        for f in store.recall(owner, topic, limit=5)
        if f.erasable_by(owner) and f.state != "quarantined"
    ]
        if len(matches) != 1:
            return FastResult.miss()  # none or ambiguous — the LLM tool can clarify
        memory.mark_if_sensitive(matches)
        store.forget(owner, matches[0].id)
        return FastResult.handled(f"Forgotten: {matches[0].text}", _VOICE_PROMPT)

    return FastResult.miss()
