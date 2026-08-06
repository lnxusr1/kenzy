"""Tier A safety announcements (5.0.6) — the first thing that speaks unprompted.

Sits between the HA event hose (:mod:`kenzy.server.ha_events`) and the policy
gate (:mod:`kenzy.server.proactive`). A hazard entity asserts, the gate decides,
and if it says yes Kenzy says so out loud in every room.

Two rules shape everything here:

**She relays; she never concludes.** Every entity in the safety map is a device
asserting a hazard — a smoke sensor reading ``on``, an alarm panel reading
``triggered``. Nothing in this module infers a hazard from a combination of
states, and that boundary is what makes speaking unprompted defensible at all.

**Release is load-bearing.** Silencing an alert lasts until the condition
*cycles*, so :meth:`SafetyWatcher.consider` calling ``gate.clear()`` on release
is the only thing that ever re-arms it. An entity going ``unavailable`` is NOT a
release — a flat battery is not a fire going out, and treating it as one would
let a flapping sensor defeat silence entirely.

The decision is pure (:meth:`consider`) and delivery is a callback, so the
interesting half is testable without a house or an event loop.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kenzy.server.proactive import SAFETY, Decision, ProactiveGate

log = logging.getLogger(__name__)

#: States that mean "this entity is not telling us anything right now". Neither
#: an assertion nor a release — see the module docstring.
_NO_INFORMATION = ("unavailable", "unknown", "")


@dataclass(frozen=True)
class Announcement:
    """What to say, and where. ``rooms`` empty ⇒ everywhere."""

    key: str
    text: str
    rooms: tuple[str, ...] = ()
    alert: bool = True


def compose(hazard: str, room_name: str) -> str:
    """The spoken sentence. One frame for every hazard, no branching on type —
    which is why the map phrases an alarm panel as "an alarm going off" rather
    than "the alarm"."""
    where = f" in the {room_name.lower()}" if room_name else ""
    return f"There's {hazard}{where}."


class SafetyWatcher:
    """Turns hazard state changes into gated announcements."""

    def __init__(
        self,
        gate: ProactiveGate,
        on_decision: Callable[[str, Decision, str], None] | None = None,
    ) -> None:
        self._gate = gate
        self._map: dict[str, dict[str, Any]] = {}
        #: Called for EVERY hazard assertion the gate ruled on — refusals
        #: included. "Why didn't she say anything?" is only answerable from a
        #: record that keeps the noes.
        self._on_decision = on_decision

    def set_map(self, mapping: dict[str, dict[str, Any]] | None) -> None:
        """Adopt a fresh safety map (same lifecycle as the occupancy map).

        Entities that vanish from the map are forgotten rather than left
        asserted forever — an operator excluding a lying sensor must not
        strand a silence that nothing can now clear.
        """
        new = dict(mapping or {})
        for gone in set(self._map) - set(new):
            self._gate.clear(gone)
        self._map = new

    def known(self) -> int:
        return len(self._map)

    def consider(
        self, entity_id: str, state: str, *, now: float | None = None
    ) -> Announcement | None:
        """Pure decision for one state change. None ⇒ say nothing."""
        entry = self._map.get(entity_id)
        if entry is None:
            return None

        value = (state or "").strip().lower()
        if value in _NO_INFORMATION:
            return None  # not a release; see module docstring

        asserted = str(entry.get("asserted") or "on").strip().lower()
        if value != asserted:
            # Back to normal. Forgetting it here is what lets the NEXT genuine
            # assertion speak immediately instead of inheriting the previous
            # event's silence.
            self._gate.clear(entity_id)
            return None

        # Tier A speaks in EVERY room, not the room the sensor is in: the point
        # is to reach whoever is home, who is rarely standing next to the smoke.
        # The room only shapes the words.
        decision = self._gate.evaluate(SAFETY, entity_id, (), now=now)
        text = compose(
            str(entry.get("hazard") or "an alert"), str(entry.get("room_name") or "")
        )
        if self._on_decision is not None:
            try:
                self._on_decision(entity_id, decision, text)
            except Exception:  # the audit trail must never break the alert
                log.debug("proactive decision listener error", exc_info=True)
        if not decision.allowed:
            log.info("Safety: not announcing %s (%s)", entity_id, decision.reason)
            return None

        return Announcement(
            key=entity_id,
            text=text,
            rooms=decision.rooms,
            alert=decision.alert,
        )

    def spoken(self, key: str, *, now: float | None = None) -> None:
        """Delivery succeeded — start the repeat window.

        Separate from :meth:`consider` for the same reason the gate splits
        evaluate/commit: an announcement that never reached a speaker must not
        suppress the next attempt.
        """
        self._gate.commit(key, now=now)
