"""
Schedule store + firing loop for timers, alarms, and reminders.

The server is the only component with a persistent clock, the node connections,
and the announce/TTS pipeline, so scheduling lives here. Entries are persisted
to a JSON file in the config home (atomic rewrite) so a timer survives a server
restart — a timer that dies with a restart is worse than no timer.

Kinds
-----
timer     — relative ("10 minutes from now"); fires once, label optional.
alarm     — clock time ("7:00"), optionally recurring on weekdays; rings until
            acknowledged (the ring loop lives in the server, not here).
reminder  — spoken text at a relative or clock time; may recur like an alarm.
command   — a deferred voice command ("turn on the lights in 30 seconds"):
            the label is the utterance, replayed through the normal intent
            pipeline at fire time. Deliberately **one-shot only** — a recurring
            command is a standing automation, which is Home Assistant's job.

Recurrence is deliberately a small rule — a set of weekdays — not cron.
All clock math uses the server's local timezone (the house clock).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

KINDS = ("timer", "alarm", "reminder", "command")

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_ALIASES = {
    "monday": "mon",
    "tuesday": "tue",
    "wednesday": "wed",
    "thursday": "thu",
    "friday": "fri",
    "saturday": "sat",
    "sunday": "sun",
}
_DAY_GROUPS = {
    "daily": list(DAY_NAMES),
    "everyday": list(DAY_NAMES),
    "weekdays": list(DAY_NAMES[:5]),
    "weekday": list(DAY_NAMES[:5]),
    "weekends": list(DAY_NAMES[5:]),
    "weekend": list(DAY_NAMES[5:]),
}

# One-shot entries whose fire time passed while the server was down are still
# fired on boot if they were missed by less than this (a late kitchen timer or
# reminder beats a silently dropped one); older ones are dropped with a log.
_MISSED_GRACE_S = 300.0

_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_hhmm(value: str) -> tuple[int, int]:
    """Parse a 24-hour ``HH:MM`` string; raises ValueError when malformed."""
    m = _HHMM_RE.match(value.strip())
    if not m:
        raise ValueError(f"not a HH:MM time: {value!r}")
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        raise ValueError(f"not a valid time of day: {value!r}")
    return hh, mm


def normalize_days(raw: Any) -> list[str]:
    """Normalize a days spec (list or comma string; aliases + group words) to
    a sorted subset of DAY_NAMES. Empty input ⇒ [] (one-shot). Unknown tokens
    raise ValueError so a bad spec fails loudly at set time, not at 7am."""
    if raw is None:
        return []
    tokens = (
        [t.strip().lower() for t in raw.split(",")]
        if isinstance(raw, str)
        else [str(t).strip().lower() for t in raw]
    )
    days: set[str] = set()
    for tok in tokens:
        if not tok:
            continue
        tok = tok.replace("every ", "").strip()
        if tok in _DAY_GROUPS:
            days.update(_DAY_GROUPS[tok])
        elif tok in DAY_NAMES:
            days.add(tok)
        elif tok in _DAY_ALIASES:
            days.add(_DAY_ALIASES[tok])
        else:
            raise ValueError(f"unknown day: {tok!r}")
    return [d for d in DAY_NAMES if d in days]


def next_occurrence(hh: int, mm: int, days: list[str], now: datetime | None = None) -> datetime:
    """Next local datetime matching ``HH:MM`` (and one of ``days`` if given)."""
    now = now or datetime.now().astimezone()
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    wanted = {DAY_NAMES.index(d) for d in days} if days else None
    for _ in range(8):  # today + a full week always contains a match
        if candidate > now and (wanted is None or candidate.weekday() in wanted):
            return candidate
        candidate += timedelta(days=1)
    raise ValueError("no next occurrence found")  # pragma: no cover - unreachable


@dataclass
class Entry:
    """One scheduled item. ``label`` is the timer name, the reminder text, or
    the deferred command's utterance."""

    id: str
    kind: str  # timer | alarm | reminder | command
    label: str
    node_id: str  # delivery target when connected (room name is the fallback)
    room: str  # human room name at set time (resolution + display)
    fire_at: float  # epoch seconds
    created_at: float
    at: str = ""  # "HH:MM" for clock-based entries (recurrence recompute)
    days: list[str] = field(default_factory=list)  # empty = one-shot
    # Who set it (speaker-ID name at set time; "" if unknown). Replayed for
    # deferred commands so speaker-gated skills see the authorizing voice.
    speaker: str = ""

    @property
    def recurring(self) -> bool:
        return bool(self.days)

    def seconds_left(self, now: float | None = None) -> float:
        return max(0.0, self.fire_at - (now if now is not None else time.time()))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Entry:
        return cls(
            id=str(d["id"]),
            kind=str(d["kind"]),
            label=str(d.get("label", "")),
            node_id=str(d.get("node_id", "")),
            room=str(d.get("room", "")),
            fire_at=float(d["fire_at"]),
            created_at=float(d.get("created_at", 0.0)),
            at=str(d.get("at", "")),
            days=[str(x) for x in (d.get("days") or [])],
            speaker=str(d.get("speaker", "")),
        )


class Scheduler:
    """Persistent schedule store + asyncio firing loop.

    ``fire_cb(entry)`` is awaited for each due entry; it must return quickly
    (spawn a task for anything long-running, e.g. an alarm ring loop). After
    firing, one-shot entries are removed and recurring ones advance to their
    next occurrence.
    """

    def __init__(self, path: Path, fire_cb: Callable[[Entry], Awaitable[None]]) -> None:
        self._path = path
        self._fire_cb = fire_cb
        self._entries: dict[str, Entry] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # Change observers (add/cancel/fire) — the dashboard's live-push hook.
        # Empty list ⇒ zero overhead, same discipline as the server's listeners.
        self._listeners: list[Callable[[], None]] = []
        self._load()

    def add_listener(self, cb: Callable[[], None]) -> None:
        """Register a callback fired (synchronously, in-loop) whenever the entry
        set changes — added, cancelled, fired, or advanced."""
        self._listeners.append(cb)

    def _notify(self) -> None:
        for cb in self._listeners:
            try:
                cb()
            except Exception as exc:  # an observer must never break the scheduler
                log.warning("Schedule listener raised: %s", exc)

    # -- persistence ---------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text())
        except FileNotFoundError:
            return
        except Exception as exc:
            log.warning("Could not read schedule store %s: %s", self._path, exc)
            return
        now = time.time()
        dropped = 0
        for item in raw if isinstance(raw, list) else []:
            try:
                entry = Entry.from_dict(item)
            except Exception:
                dropped += 1
                continue
            if entry.fire_at <= now:
                if entry.recurring:
                    # Advance a recurring entry past the downtime to its next slot.
                    try:
                        hh, mm = parse_hhmm(entry.at)
                        entry.fire_at = next_occurrence(hh, mm, entry.days).timestamp()
                    except ValueError:
                        dropped += 1
                        continue
                elif now - entry.fire_at > _MISSED_GRACE_S or entry.kind == "alarm":
                    # A stale timer/reminder, or any missed one-shot alarm: drop.
                    dropped += 1
                    continue
                # else: recently missed timer/reminder — keep, fires immediately (late).
            self._entries[entry.id] = entry
        if self._entries or dropped:
            log.info(
                "Schedule store loaded: %d entr%s (%d dropped as stale/invalid)",
                len(self._entries),
                "y" if len(self._entries) == 1 else "ies",
                dropped,
            )

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps([e.to_dict() for e in self._entries.values()], indent=2))
            tmp.replace(self._path)
        except OSError as exc:
            log.error("Could not persist schedule store %s: %s", self._path, exc)

    # -- store API ------------------------------------------------------

    def add(
        self,
        kind: str,
        node_id: str,
        room: str,
        *,
        label: str = "",
        seconds: float | None = None,
        at: str = "",
        days: list[str] | None = None,
        speaker: str = "",
    ) -> Entry:
        """Validate + store a new entry; returns it. Raises ValueError on a bad spec."""
        if kind not in KINDS:
            raise ValueError(f"unknown schedule kind: {kind!r}")
        days = normalize_days(days)
        if kind == "command":
            if not label.strip():
                raise ValueError("a scheduled command needs the command text")
            if days:
                # A recurring command is a standing automation — Home Assistant's job.
                raise ValueError("a scheduled command cannot recur")
        if seconds is not None:
            if days:
                raise ValueError("a relative timer cannot recur")
            if not 0 < seconds <= 7 * 24 * 3600:
                raise ValueError("duration out of range")
            fire_at = time.time() + float(seconds)
            at = ""
        elif at:
            hh, mm = parse_hhmm(at)
            fire_at = next_occurrence(hh, mm, days).timestamp()
        else:
            raise ValueError("a schedule needs either a duration or a time")
        entry = Entry(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            label=label.strip()[:200],
            node_id=node_id,
            room=room,
            fire_at=fire_at,
            created_at=time.time(),
            at=at,
            days=days,
            speaker=speaker.strip(),
        )
        self._entries[entry.id] = entry
        self._save()
        self._wake.set()
        self._notify()
        log.info(
            "[%s] scheduled %s %r → %s%s",
            room or node_id,
            kind,
            entry.label,
            datetime.fromtimestamp(fire_at).astimezone().isoformat(timespec="seconds"),
            f" (every {','.join(days)})" if days else "",
        )
        return entry

    def cancel(self, ids: list[str]) -> list[Entry]:
        """Remove entries by id; returns the removed entries."""
        removed = [self._entries.pop(i) for i in ids if i in self._entries]
        if removed:
            self._save()
            self._wake.set()
            self._notify()
            for e in removed:
                log.info("[%s] cancelled %s %r", e.room or e.node_id, e.kind, e.label)
        return removed

    def entries(self, node_id: str | None = None) -> list[Entry]:
        """All entries (or one node's), soonest first."""
        out = [e for e in self._entries.values() if node_id is None or e.node_id == node_id]
        return sorted(out, key=lambda e: e.fire_at)

    # -- firing loop ------------------------------------------------------

    async def fire_due(self, now: float | None = None) -> list[Entry]:
        """Fire every due entry (awaiting ``fire_cb``), then advance/remove it.

        Split out from the loop so tests can drive it directly with a fake clock.
        """
        now = time.time() if now is None else now
        due = [e for e in self._entries.values() if e.fire_at <= now]
        for entry in sorted(due, key=lambda e: e.fire_at):
            try:
                await self._fire_cb(entry)
            except Exception as exc:  # a delivery failure must never kill the loop
                log.error("Schedule fire failed for %s: %s", entry.id, exc, exc_info=True)
            if entry.recurring:
                hh, mm = parse_hhmm(entry.at)
                entry.fire_at = next_occurrence(hh, mm, entry.days).timestamp()
            else:
                self._entries.pop(entry.id, None)
        if due:
            self._save()
            self._notify()
        return due

    async def _run(self) -> None:
        while True:
            pending = self.entries()
            timeout = min((e.fire_at for e in pending), default=time.time() + 3600) - time.time()
            self._wake.clear()
            if timeout > 0:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=min(timeout, 3600.0))
                    continue  # woken by add/cancel — recompute the next deadline
                except TimeoutError:
                    pass
            await self.fire_due()

    def start(self) -> None:
        """Start the firing loop (requires a running event loop). Idempotent."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="scheduler")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
