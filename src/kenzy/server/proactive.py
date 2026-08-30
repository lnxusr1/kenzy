"""Proactive speech — the policy gate (5.0.6).

The first thing in Kenzy that talks without being talked to. Everything about
this module is built around one asymmetry: a *missed* announcement is a bad
feature, but a *wrong* one is a house that shouts at you, and people forgive
the first far more readily than the second.

So the shape is **default-deny**. Nothing is announced unless its category was
switched on deliberately, and every decision — allowed or refused — carries a
reason, because "why didn't she say anything?" is exactly as important a
question as "why did she just say that?" and only one of them is answerable
from a log that records successes.

**The gate governs Kenzy's INITIATIVE, not your INSTRUCTIONS.** An announce you
typed, or one your Home Assistant automation sent to the MQTT announce topic,
is you speaking through her and does not come through here — quiet hours must
not silence a message you deliberately sent, and a rate limit must not swallow
your second call to dinner. What this module governs is the case where a sensor
changed and *she* decided that was worth saying out loud.

Pure policy: no I/O, no server import, no delivery. It answers "may this be
said, where, and why" and nothing else — the same split as ``calibration.py``,
and for the same reason: the interesting logic is the part you can test without
a house attached.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

#: Refusal reasons. Stable strings — they are written to the audit trail and
#: shown in the dashboard, so treat them as a wire contract, not log prose.
DENY_DISABLED = "proactive speech is off"
DENY_CATEGORY_OFF = "category not enabled"
DENY_REPEAT = "already announced recently"
DENY_ACKNOWLEDGED = "acknowledged"
DENY_RATE = "rate limit reached"
DENY_QUIET_HOURS = "quiet hours"
DENY_NO_ROOMS = "no eligible rooms"


@dataclass(frozen=True)
class Category:
    """A kind of unprompted speech, and what it is allowed to override.

    The overrides are DATA, not branches on a tier name. Tier A's exemptions
    are the whole reason it exists — a smoke alert that respects quiet hours is
    not a safety feature, it is a decoration — and writing them as fields means
    the next tier declares its own posture instead of inheriting one by
    accident. Tier B, when it lands, simply sets none of them.
    """

    name: str
    #: Speak even between quiet hours. True for safety: fires are nocturnal.
    ignores_quiet_hours: bool = False
    #: Speak even in rooms marked do-not-disturb.
    ignores_dnd: bool = False
    #: Speak on a muted node (delivery rides the existing alert-audio floor,
    #: the same path the wake chime uses on a muted node).
    ignores_mute: bool = False


#: Tier A — safety. Smoke, water leak, and an alarm panel already in
#: ``triggered``. Every one of those is an assertion a DEVICE made; Kenzy
#: relays it and infers nothing. That boundary is what makes the tier safe to
#: ship, and anything that would require her to *conclude* something ("a door
#: opened and everyone seems to be out, so this is an intruder") belongs to a
#: later tier with a lot more thought behind it.
SAFETY = Category(
    "safety",
    ignores_quiet_hours=True,
    ignores_dnd=True,
    ignores_mute=True,
)

#: Task results — SOLICITED unprompted speech ("go do X and let me know when
#: you're done"): the user asked for exactly this delivery, but it still
#: respects every policy knob — no exemptions, making this the first
#: non-exempt category, the one that turns quiet_hours / dnd_rooms /
#: rate_limit from configurable-and-inert into real. Off by default like all
#: proactive speech (`proactive.tasks.enabled`); an undeliverable result
#: waits in the ledger and is picked up at the start of the owner's next
#: conversation instead — denied is never dropped.
TASKS = Category("tasks")

CATEGORIES: dict[str, Category] = {SAFETY.name: SAFETY, TASKS.name: TASKS}


@dataclass(frozen=True)
class Decision:
    """The gate's answer. ``reason`` is populated either way — see module doc."""

    allowed: bool
    reason: str
    #: Rooms that may hear it (empty when refused). "" means every room.
    rooms: tuple[str, ...] = ()
    #: True ⇒ deliver at the alert floor so a muted node still plays it.
    alert: bool = False

    def as_record(self) -> dict[str, Any]:
        """Audit-trail shape. Kept small and flat — this is written on EVERY
        evaluation, including refusals, and it must survive `dashboard.logs`
        being off: it is Kenzy's own conduct, not household speech."""
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "rooms": list(self.rooms),
            "alert": self.alert,
        }


def parse_quiet_hours(spec: str) -> tuple[int, int] | None:
    """``"22:00-07:00"`` → (1320, 420) as minutes past midnight, or None.

    Wrapping past midnight is the normal case, not the edge case, so the
    comparison in :meth:`ProactiveGate.evaluate` handles start > end rather
    than pretending it can't happen.
    """
    text = (spec or "").strip()
    if not text or "-" not in text:
        return None
    start_s, _, end_s = text.partition("-")

    def _minutes(v: str) -> int | None:
        v = v.strip()
        if ":" not in v:
            return None
        h, _, m = v.partition(":")
        try:
            hh, mm = int(h), int(m)
        except ValueError:
            return None
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        return hh * 60 + mm

    start, end = _minutes(start_s), _minutes(end_s)
    if start is None or end is None or start == end:
        return None
    return start, end


@dataclass
class ProactiveGate:
    """Evaluates whether an unprompted utterance may be spoken.

    Holds only the small amount of state a policy needs to be honest about
    repetition and rate: recent utterance timestamps, and when each distinct
    condition was last announced.
    """

    enabled: bool = False
    quiet_hours: tuple[int, int] | None = None
    dnd_rooms: frozenset[str] = frozenset()
    rate_limit: int = 6
    rate_window: float = 3600.0
    #: Per-category on/off. Absent ⇒ off. Default-deny is the whole point.
    categories_enabled: frozenset[str] = frozenset()
    #: Per-category seconds before the SAME condition may be announced again.
    repeat_after: dict[str, float] = field(default_factory=dict)
    _recent: deque[float] = field(default_factory=deque, init=False, repr=False)
    _last_said: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    #: Silenced conditions. Not a timer — the ONLY thing that re-arms one is the
    #: sensor releasing (:meth:`clear`), i.e. the condition actually cycling.
    _acked: set[str] = field(default_factory=set, init=False, repr=False)

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None) -> ProactiveGate:
        """Build from the ``proactive`` block of server.yaml."""
        cfg = cfg or {}
        enabled_cats: set[str] = set()
        repeat: dict[str, float] = {}
        for name in CATEGORIES:
            block = cfg.get(name) or {}
            if bool(block.get("enabled", False)):
                enabled_cats.add(name)
            repeat[name] = float(block.get("repeat_after", 300))
        return cls(
            enabled=bool(cfg.get("enabled", True)),
            quiet_hours=parse_quiet_hours(str(cfg.get("quiet_hours", "") or "")),
            dnd_rooms=frozenset(str(r) for r in (cfg.get("dnd_rooms") or [])),
            rate_limit=int(cfg.get("rate_limit", 6)),
            rate_window=float(cfg.get("rate_window", 3600)),
            categories_enabled=frozenset(enabled_cats),
            repeat_after=repeat,
        )

    def evaluate(
        self,
        category: Category,
        key: str,
        rooms: tuple[str, ...] = (),
        *,
        now: float | None = None,
        local_minutes: int | None = None,
    ) -> Decision:
        """May this be said?

        ``key`` identifies the CONDITION, not the utterance — e.g. the entity
        that asserted it. Two different phrasings of the same smoke alarm must
        share a key or repeat-suppression cannot work.

        ``local_minutes`` is minutes past local midnight; derived from ``now``
        when omitted. Injectable so quiet-hours tests don't depend on the clock
        of whoever runs them.
        """
        now = time.time() if now is None else now

        if not self.enabled:
            return Decision(False, DENY_DISABLED)
        if category.name not in self.categories_enabled:
            return Decision(False, DENY_CATEGORY_OFF)

        if key in self._acked:
            return Decision(False, DENY_ACKNOWLEDGED)

        # Repetition before rate: a sensor that flaps should not consume the
        # hour's whole budget and mute a genuinely different alert behind it.
        gap = self.repeat_after.get(category.name, 300.0)
        last = self._last_said.get(key)
        if last is not None and gap > 0 and now - last < gap:
            return Decision(False, DENY_REPEAT)

        self._prune(now)
        if self.rate_limit > 0 and len(self._recent) >= self.rate_limit:
            return Decision(False, DENY_RATE)

        if self.quiet_hours is not None and not category.ignores_quiet_hours:
            minutes = self._local_minutes(now) if local_minutes is None else local_minutes
            start, end = self.quiet_hours
            if start < end:
                inside = start <= minutes < end
            else:  # the window wraps past midnight — the normal case
                inside = minutes >= start or minutes < end
            if inside:
                return Decision(False, DENY_QUIET_HOURS)

        allowed_rooms = rooms
        if self.dnd_rooms and not category.ignores_dnd:
            allowed_rooms = tuple(r for r in rooms if r not in self.dnd_rooms)
            # An empty `rooms` means "everywhere", and DND can't narrow a set it
            # was never given — the caller has to name rooms for DND to bite.
            if rooms and not allowed_rooms:
                return Decision(False, DENY_NO_ROOMS)

        return Decision(True, "allowed", allowed_rooms, alert=category.ignores_mute)

    def commit(self, key: str, *, now: float | None = None) -> None:
        """Record that an allowed utterance was actually delivered.

        Separate from :meth:`evaluate` on purpose: a decision that never
        reaches a speaker must not consume the rate budget or start a repeat
        window, or one broken TTS service would silently suppress the next
        genuine alert.
        """
        now = time.time() if now is None else now
        self._last_said[key] = now
        self._recent.append(now)
        self._prune(now)

    def acknowledge(self, key: str | None = None, *, now: float | None = None) -> list[str]:
        """"I've heard it" — silence a live condition. Returns the keys silenced.

        ``key=None`` silences everything currently live, which is what a person
        interacting with Kenzy means: they are awake, in earshot of something
        that spoke in every room, and do not need telling again.

        **Silence lasts until the condition CYCLES**, not for a timer. The only
        thing that re-arms it is the sensor releasing and re-asserting
        (:meth:`clear`). A snooze would be worse on both ends — it nags someone
        who already told Kenzy they know, and it makes "is it silenced?"
        unanswerable without also knowing what time it is. This mirrors how a
        physical panel behaves when you hit silence.

        The condition is NOT forgotten: the sensor is still asserting and the
        dashboard still shows it. Silenced is a state you can see, not an
        absence.

        **Wire this to the event that actually fires.** In 5.0.5 the ringing
        alarm's acknowledgement hung off ``on_wakeword``, which never fires
        while audio is playing — the node stops its own playback and opens a
        fresh session instead — so a wake word could not stop a ringing alarm
        from the day alarms shipped. Anything that repeats needs its stop wired
        to ``on_session_start`` (and it costs nothing to wire both).
        """
        keys = [key] if key is not None else list(self._last_said)
        self._acked.update(keys)
        return keys

    def silenced(self) -> list[str]:
        """Live conditions the household has silenced — a dashboard state."""
        return sorted(self._acked)

    def clear(self, key: str) -> None:
        """The condition released (sensor back to normal).

        Forgets it entirely — including any silence — so a genuine
        re-assertion speaks IMMEDIATELY rather than inheriting a window from
        the previous event. **This is the only thing that undoes a silence**,
        which is what makes "off, then on again" the honest signal of a new
        event rather than a continuation of the old one. A second fire is not a
        repeat of the first.
        """
        self._last_said.pop(key, None)
        self._acked.discard(key)

    def live(self) -> list[str]:
        """Conditions announced and not yet cleared — what an ack would cover."""
        return list(self._last_said)

    def _prune(self, now: float) -> None:
        while self._recent and now - self._recent[0] >= self.rate_window:
            self._recent.popleft()

    @staticmethod
    def _local_minutes(now: float) -> int:
        lt = time.localtime(now)
        return lt.tm_hour * 60 + lt.tm_min
