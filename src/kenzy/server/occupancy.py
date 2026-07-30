"""The occupancy tracker — the v5 spine, Slice B.

Two decaying quantities per room, and the whole point is that they are *honest*:

- **occupancy confidence** — slow decay, from mmWave/PIR. "Someone is here."
- **identity confidence** — fast decay, from voice. "…and it was Alice, 20s ago."

They anchor each other. mmWave says the room is still occupied; voice says who
it most recently was. Within the voice window that buys a real (if brief)
identity dimension with no new hardware; silent people and minutes-old positions
wait for the 5.2 identity tier.

Three rules this module exists to enforce
-----------------------------------------
**Absence is not a value.** No sensor and no recent voice means *unknown*, not
*empty*. Everything downstream in 5.0.1 (empty-room endpointing, occupied-room
targeting) hangs off that distinction, and collapsing it means she stops talking
to rooms that have people in them.

**Pulse and level are different physics.** A PIR *pulses* — it fires on motion
and goes quiet while you sit perfectly still, so its evidence must decay. mmWave
still-presence and ``person.*`` home/away *assert a level* — they keep saying
"occupied" until they say otherwise, so their evidence must NOT drain while the
assertion stands and the socket is healthy. Modelling both as "decaying
confidence" would make a healthy sensor fade to unknown just because time passed.

**Staleness is not absence.** When the HA socket drops, held levels stop being
trustworthy — but they don't become evidence of an empty room. The tracker
reports them as stale and lets the caller decide, rather than inventing a fact.
The same applies one sensor at a time: a level that stops reporting (flat
battery, entity deleted, sensor excluded from the map) has its hold faded out
rather than either believed forever or slammed to unknown — see ``_drop_hold``.

Nothing is persisted. ``get_states`` re-seeds level evidence at every reconnect
(see :mod:`kenzy.server.ha_events`), and voice re-learns within its window, so a
restart costs seconds of history rather than requiring a store that could serve a
stale picture as current.
"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: Half-life of a motion PULSE. A PIR that fired 30s ago is decent evidence; one
#: that fired 5 minutes ago is not, because you may simply have left.
_PULSE_HALFLIFE_S = 180.0

#: Half-life of a VOICE identity anchor — deliberately much faster. Knowing who
#: spoke is only trustworthy for about as long as a conversation.
_VOICE_HALFLIFE_S = 90.0

#: After a level RELEASES ("clear"), how fast the residual belief fades. Not
#: instant: mmWave clearing usually means the person left, but a brief drop-out
#: while they sat still shouldn't slam the room to unknown.
_RELEASE_HALFLIFE_S = 60.0

#: Sources that mean "a level stopped asserting" rather than "a pulse fired", and
#: therefore fade on `_RELEASE_HALFLIFE_S`. "released" = the sensor said clear;
#: "dropout" = it stopped saying anything at all (see `_drop_hold`).
_FADING_SOURCES = ("released", "dropout")

#: Confidence at or above this reads as "occupied"; below `_UNKNOWN_FLOOR` reads
#: as "unknown". The band between them is deliberate: it's "probably, recently".
_OCCUPIED_THRESHOLD = 0.5
_UNKNOWN_FLOOR = 0.05


def room_slug(text: str) -> str:
    """Normalize a room/area name to the join key.

    The join is HA-area ↔ Kenzy-room ("Office" ↔ "office"), and it has to happen
    somewhere that sees both sides — the server owns the canonical room names,
    so it happens here. Must stay byte-compatible with ``ha_model._slug``, which
    produces the map's keys.
    """
    cleaned = re.sub(r"[^\w\s]", "", text or "").strip().lower()
    return re.sub(r"\s+", "_", cleaned)


def decay(value: float, elapsed: float, halflife: float) -> float:
    """Exponential decay. Pure, so the curves are exhaustively testable.

    Chosen over linear because the felt behavior is "confidence fades fast at
    first, then lingers" — and because a half-life is a number you can reason
    about out loud ("a PIR hit is worth half as much three minutes later").
    """
    if value <= 0.0 or elapsed <= 0.0:
        return max(0.0, value)
    if halflife <= 0.0:
        return 0.0
    return value * math.pow(0.5, elapsed / halflife)


@dataclass
class RoomBelief:
    """What the tracker believes about one room, and why."""

    room: str
    #: Confidence [0,1] that a person is present, with when it was last fed.
    occupancy: float = 0.0
    occupancy_at: float = 0.0
    occupancy_source: str = ""
    #: Entities currently ASSERTING a level (mmWave saying "occupied"). While
    #: non-empty and the socket is healthy, occupancy is pinned — no decay.
    held: set[str] = field(default_factory=set)
    #: The voice anchor: who was last heard here, and how sure we are now.
    identity: float = 0.0
    identity_at: float = 0.0
    person_id: str = ""
    person_name: str = ""

    def occupancy_now(self, now: float, *, stale: bool = False) -> float:
        """Current occupancy confidence, decayed — unless a level is asserting."""
        if self.held and not stale:
            return 1.0  # a healthy sensor still says "occupied"; belief holds
        fading = self.occupancy_source in _FADING_SOURCES
        halflife = _RELEASE_HALFLIFE_S if fading else _PULSE_HALFLIFE_S
        return decay(self.occupancy, now - self.occupancy_at, halflife)

    def identity_now(self, now: float) -> float:
        return decay(self.identity, now - self.identity_at, _VOICE_HALFLIFE_S)


class OccupancyTracker:
    """Per-room belief, fed by HA evidence and voice sessions.

    ONE tracker — never a parallel identity system. It consumes the server's
    existing session hooks for the voice half and :mod:`ha_events`' seam for the
    sensor half, and answers snapshot() for the Presence view and request injection.
    """

    def __init__(self) -> None:
        self._rooms: dict[str, RoomBelief] = {}
        #: House-scope evidence: person entity → (home?, when, display name).
        self._house: dict[str, dict[str, Any]] = {}
        self._stale = False

    # -- feeds --------------------------------------------------------------

    def on_evidence(self, ev: Any) -> None:
        """Consume one :class:`kenzy.server.ha_events.Evidence`."""
        if getattr(ev, "scope", "room") == "house":
            if not getattr(ev, "available", True):
                # A `person.*` entity that stopped reporting is NOT someone who
                # left — writing `home: False` here would invent exactly the kind
                # of fact this module refuses to. Keep the last known answer; its
                # age is already reported, so the staleness shows.
                return
            self._house[ev.entity_id] = {
                "home": bool(ev.present),
                "ts": ev.ts,
                "name": getattr(ev, "name", "") or "",
            }
            return
        room = getattr(ev, "room", "")
        if not room:
            return
        belief = self._rooms.setdefault(room, RoomBelief(room=room))
        if ev.kind == "level":
            if not getattr(ev, "available", True):
                # The sensor stopped reporting at all (flat battery, entity
                # deleted). Its assertion can no longer be trusted, and NOTHING
                # else will ever release it — HA reports the corpse as
                # `unavailable` and a deleted entity as `new_state: null`, both
                # of which are (rightly) not evidence of absence. Without this
                # the hold pins the room "occupied" at full confidence forever.
                self._drop_hold(belief, ev.entity_id, ev.ts, "dropout")
            elif ev.present:
                belief.held.add(ev.entity_id)
                belief.occupancy = 1.0
                belief.occupancy_at = ev.ts
                belief.occupancy_source = ev.entity_id
            else:
                self._drop_hold(belief, ev.entity_id, ev.ts, "released")
        elif ev.present:  # pulse: only a positive edge is evidence
            belief.occupancy = 1.0
            belief.occupancy_at = ev.ts
            belief.occupancy_source = ev.entity_id

    @staticmethod
    def _drop_hold(belief: RoomBelief, entity_id: str, ts: float, source: str) -> None:
        """One entity stops asserting a level. The only path out of ``held``.

        Two rules live here, both learned the hard way:

        **Never asserted ⇒ nothing happened.** The common case at seed time is a
        sensor that is simply idle, and that is evidence of ABSENCE, not of a
        recent departure. Treating it as a release made every room with a quiet
        motion sensor read "occupied" for minutes after startup.

        **Still held by another sensor ⇒ belief stays pinned.** Only the last
        assertion to drop starts the fade, and it fades from full rather than
        slamming to unknown on a momentary drop-out while someone sits still.
        """
        if entity_id not in belief.held:
            return
        belief.held.discard(entity_id)
        if belief.held:
            return
        belief.occupancy = 1.0
        belief.occupancy_at = ts
        belief.occupancy_source = source

    def prune_held(self, valid: set[str]) -> None:
        """The evidence map changed — forget entities it no longer covers.

        Curation excluding a lying sensor (or an entity disappearing from HA
        between reconnects) removes it from the map, after which it can never
        emit again — so an assertion it left behind would be held forever, the
        same stuck-pin as a dead sensor. Fades those holds out instead, and
        drops house-scope people the map no longer carries so the Presence view
        stops listing someone who is no longer tracked.

        **Invariant this relies on:** every map refetch is followed by a
        ``get_states`` seed in the same session (``ha_events._session``), which
        re-establishes level state for everything the map still covers. Dropping
        people is only safe because of that — ``person.*`` emits solely on
        CHANGE, so pruning one without a reseed would blank it until whenever
        they next come or go. Don't call this from anywhere that doesn't reseed.
        """
        now = time.monotonic()
        for belief in self._rooms.values():
            for entity_id in sorted(belief.held - valid):
                self._drop_hold(belief, entity_id, now, "dropout")
        for entity_id in [e for e in self._house if e not in valid]:
            self._house.pop(entity_id, None)

    def on_voice(
        self,
        room: str,
        *,
        person_id: str = "",
        person_name: str = "",
        recognized: bool = False,
        ts: float | None = None,
    ) -> None:
        """A voice session happened in ``room`` — the identity anchor.

        Someone spoke, so the room is occupied regardless of whether we know who
        (an unknown voice is still a person). Identity confidence is only raised
        when the speaker was actually recognized.
        """
        if not room:
            return
        now = time.monotonic() if ts is None else ts
        belief = self._rooms.setdefault(room, RoomBelief(room=room))
        belief.occupancy = 1.0
        belief.occupancy_at = now
        belief.occupancy_source = "voice"
        if recognized and person_id:
            belief.identity = 1.0
            belief.identity_at = now
            belief.person_id = person_id
            belief.person_name = person_name

    def set_stale(self, stale: bool) -> None:
        """The HA socket's health. Stale means held levels stop being trusted."""
        self._stale = bool(stale)

    # -- reads --------------------------------------------------------------

    def room_state(self, room: str, now: float | None = None) -> dict[str, Any]:
        """One room's belief, with provenance and age — never a bare boolean."""
        now = time.monotonic() if now is None else now
        belief = self._rooms.get(room)
        if belief is None:
            return {"room": room, "state": "unknown", "confidence": 0.0, "source": "", "age": None}
        conf = belief.occupancy_now(now, stale=self._stale)
        if conf >= _OCCUPIED_THRESHOLD:
            state = "occupied"
        elif conf < _UNKNOWN_FLOOR:
            # Deliberately NOT "empty": we have no evidence either way, and the
            # difference matters to every 5.0.1 behavior that reads this.
            state = "unknown"
        else:
            state = "maybe"
        out: dict[str, Any] = {
            "room": room,
            "state": state,
            "confidence": round(conf, 3),
            "source": belief.occupancy_source,
            "held": sorted(belief.held),
            "age": round(now - belief.occupancy_at, 1) if belief.occupancy_at else None,
            "stale": self._stale and bool(belief.held),
        }
        ident = belief.identity_now(now)
        if ident >= _UNKNOWN_FLOOR and belief.person_id:
            out["person_id"] = belief.person_id
            out["person_name"] = belief.person_name
            out["identity_confidence"] = round(ident, 3)
            out["identity_age"] = round(now - belief.identity_at, 1)
        return out

    def snapshot(self, rooms: list[str] | None = None, now: float | None = None) -> dict[str, Any]:
        """The whole picture: the Presence view's payload and the injected block.

        ``rooms`` (the server's connected room names) is unioned with the rooms
        the tracker has evidence for, so a room with a node but no sensor shows
        up as an honest "unknown" rather than silently missing.
        """
        now = time.monotonic() if now is None else now
        names = set(self._rooms) | {r for r in (rooms or []) if r}
        people = [
            {
                "entity_id": eid,
                "name": rec.get("name") or "",
                "home": bool(rec.get("home")),
                "age": round(now - float(rec.get("ts") or now), 1),
            }
            for eid, rec in sorted(self._house.items())
        ]
        return {
            "rooms": [self.room_state(name, now) for name in sorted(names)],
            "people": people,
            "stale": self._stale,
        }
