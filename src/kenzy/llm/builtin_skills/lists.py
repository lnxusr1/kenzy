"""
Shopping & to-do lists — the voice front-end to Home Assistant's `todo` lists.

Deliberately **HA-only, no Kenzy-side storage**: HA's `todo` entities already
sync to every phone via the companion app (which is half the point of a
shopping list), and the same entity interface covers local lists and synced
backends (Google Tasks, Todoist, CalDAV, …) alike. Kenzy adds the voice layer:
add / read / check off / remove by name, with the spoken-name → entity mapping
coming from ``curation.yaml``'s ``lists:`` block (default list + aliases),
editable in the dashboard's Home Assistant tab.

Two tiers, like home_assistant and schedule:
  * a fast intent handles the everyday phrasings instantly (no model call) —
    "add milk to the shopping list", "what's on the list", "check off milk",
    "take eggs off the list";
  * @skill tools cover the fuzzy tier ("add everything I need for pancakes").

Setup: at least one `todo` entity must exist in HA (Settings → Devices &
Services → Add Integration → "Local to-do"). With none, Kenzy says so.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

import httpx

from kenzy.llm.builtin_skills import ha_model
from kenzy.llm.skills import FastResult, ask, fast_intent, is_disabled, skill

log = logging.getLogger(__name__)

_HA_DOWN = "I couldn't reach Home Assistant to get to your lists."
_NO_LISTS = (
    "There are no to-do lists in Home Assistant yet. Add the Local to-do "
    "integration there to create one."
)
_NOT_CONFIGURED = "Lists live in Home Assistant, and the Home Assistant connection isn't set up."
_CREATE_FALLBACK = (
    "I couldn't create the list from here. You can add one in Home Assistant: "
    "Settings, Devices and Services, then add the Local to-do integration."
)


def _ha_configured() -> bool:
    """The hard gate: lists only operate when the HA connection is configured
    (HA_API_KEY set) AND the home_assistant skill hasn't been disabled."""
    return bool(os.environ.get("HA_API_KEY")) and not is_disabled("home_assistant")


# ---------------------------------------------------------------------------
# HA todo services
# ---------------------------------------------------------------------------


async def _todo_service(
    service: str, entity_id: str, data: dict[str, Any] | None = None, *, respond: bool = False
) -> dict[str, Any] | None:
    base, headers = ha_model.ha_conn()
    url = f"{base}/api/services/todo/{service}" + ("?return_response" if respond else "")
    payload: dict[str, Any] = {"entity_id": entity_id, **(data or {})}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        if not respond:
            return None
        body = resp.json()
        # REST `?return_response` wraps the payload in `service_response`.
        out = body.get("service_response", body) if isinstance(body, dict) else {}
        return out if isinstance(out, dict) else {}


async def _get_items(entity_id: str) -> list[str]:
    """Open ("needs_action") item summaries on a list, in HA's order."""
    resp = await _todo_service("get_items", entity_id, {"status": "needs_action"}, respond=True)
    items = ((resp or {}).get(entity_id) or {}).get("items") or []
    return [str(i.get("summary", "")).strip() for i in items if i.get("summary")]


async def _create_local_list(name: str) -> bool:
    """Create a Local-to-do list by driving HA's config flow. Best-effort.

    There is no ``todo.create_list`` service — lists are created by config
    entries, so this uses the config-flow REST surface the HA frontend itself
    uses. Not a guaranteed-stable contract, hence best-effort: any failure
    returns False and the caller falls back to the spoken HA instruction.
    """
    try:
        base, headers = ha_model.ha_conn()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{base}/api/config/config_entries/flow",
                headers=headers,
                json={"handler": "local_todo"},
            )
            resp.raise_for_status()
            flow_id = resp.json().get("flow_id")
            if not flow_id:
                return False
            resp = await client.post(
                f"{base}/api/config/config_entries/flow/{flow_id}",
                headers=headers,
                json={"todo_list_name": name},
            )
            resp.raise_for_status()
            return bool(resp.json().get("type") == "create_entry")
    except Exception as exc:
        log.warning("Could not create Local to-do list %r: %s", name, exc)
        return False


async def _local_entry_id(entity_id: str) -> str | None:
    """The ``local_todo`` config-entry id backing a todo entity, or None.

    None means the list is NOT ours to delete — it belongs to a synced provider
    (Google Tasks, Todoist, CalDAV…) whose config entry lives outside the
    ``local_todo`` domain, or HA couldn't be asked. Deletion is refused for both.
    """
    try:
        base, headers = ha_model.ha_conn()
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{base}/api/template",
                headers=headers,
                json={"template": "{{ config_entry_id('" + entity_id + "') }}"},
            )
            resp.raise_for_status()
            entry_id = resp.text.strip()
            if not entry_id or entry_id == "None":
                return None
            resp = await client.get(
                f"{base}/api/config/config_entries/entry?domain=local_todo",
                headers=headers,
            )
            resp.raise_for_status()
            entries = resp.json()
            if isinstance(entries, list) and any(
                isinstance(e, dict) and e.get("entry_id") == entry_id for e in entries
            ):
                return entry_id
    except Exception as exc:
        log.warning("Could not resolve config entry for %s: %s", entity_id, exc)
    return None


async def _delete_entry(entry_id: str) -> bool:
    """Delete a ``local_todo`` config entry (the inverse of ``_create_local_list``).
    Best-effort — False sends the caller to the spoken manual instruction."""
    try:
        base, headers = ha_model.ha_conn()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.delete(
                f"{base}/api/config/config_entries/entry/{entry_id}", headers=headers
            )
            resp.raise_for_status()
        return True
    except Exception as exc:
        log.warning("Could not delete to-do list entry %s: %s", entry_id, exc)
        return False


async def _create_and_add(name: str, items: list[str]) -> str:
    """Create a list (confirmed by the user), then add the originally requested
    items to it. Falls back to the spoken HA instruction when creation fails."""
    if not await _create_local_list(name):
        return _CREATE_FALLBACK
    match: dict[str, str] | None = None
    for _ in range(4):  # the new entity registers near-instantly, but be patient
        try:
            available = await ha_model.fetch_todo_lists()
        except Exception:
            available = []
        match = next(
            (e for e in available if _normalize_list_name(e["name"]) == _normalize_list_name(name)),
            None,
        )
        if match:
            break
        await asyncio.sleep(0.5)
    if match is None:
        return (
            f"I created the {name}, but it hasn't shown up yet — "
            "try adding your items again in a moment."
        )
    if not items:
        return f"Created the {match['name']}."
    added = await _do_add(match["entity_id"], match["name"], items)
    return f"Created the {match['name']}. {added}"


# ---------------------------------------------------------------------------
# Spoken-name → list resolution
# ---------------------------------------------------------------------------


def _normalize_list_name(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip().lower())
    for prefix in ("the ", "my ", "our "):
        text = text.removeprefix(prefix)
    for suffix in (" list", " lists"):
        text = text.removesuffix(suffix)
    text = text.strip()
    # A bare/generic reference ("the list", "my to-do list") means the default.
    return "" if text in ("list", "lists", "todo", "to-do", "to do") else text


def _display(available: list[dict[str, str]], entity_id: str) -> str:
    for entry in available:
        if entry["entity_id"] == entity_id:
            return entry["name"]
    return entity_id.split(".", 1)[-1].replace("_", " ")


async def _resolve_list(spoken: str) -> tuple[str | None, str | None, list[dict[str, str]]]:
    """Map a spoken list name ('' = the default) to a todo entity.

    Returns (entity_id, spoken_error, available). Exactly one of the first two
    is set; the error text is speakable as-is (HA down, no lists, ambiguous…).
    """
    try:
        available = await ha_model.fetch_todo_lists()
    except Exception as exc:
        log.warning("todo list fetch failed: %s", exc)
        return None, _HA_DOWN, []
    if not available:
        return None, _NO_LISTS, []
    ids = {entry["entity_id"] for entry in available}
    cur = ha_model.load_curation().get("lists") or {}

    wanted = _normalize_list_name(spoken)
    if not wanted:  # "the list" / "my list" / no name → the default
        default = str(cur.get("default") or "")
        if default in ids:
            return default, None, available
        if len(available) == 1:
            return available[0]["entity_id"], None, available
        names = ", ".join(entry["name"] for entry in available)
        return None, f"Which list? You have: {names}.", available

    for eid, aliases in (cur.get("aliases") or {}).items():
        if eid in ids and any(_normalize_list_name(str(a)) == wanted for a in aliases):
            return eid, None, available
    for entry in available:
        if _normalize_list_name(entry["name"]) == wanted:
            return entry["entity_id"], None, available
        if entry["entity_id"].split(".", 1)[-1].replace("_", " ") == wanted:
            return entry["entity_id"], None, available
    try:  # fuzzy last — high cutoff, the LLM is the safety net below it
        from rapidfuzz import fuzz, process

        choices = {e["entity_id"]: _normalize_list_name(e["name"]) for e in available}
        best = process.extractOne(wanted, choices, scorer=fuzz.WRatio, score_cutoff=87)
        if best:
            return str(best[2]), None, available
    except ImportError:  # pragma: no cover - rapidfuzz ships in the llm extra
        pass
    return None, None, available  # unknown name: not speakable — miss to the LLM


async def _resolve_list_strict(spoken: str) -> tuple[str | None, str | None, list[dict[str, str]]]:
    """Spoken name → todo entity for DESTRUCTIVE operations: exact display-name,
    entity-slug, or curated-alias matches only — no fuzzy matching and no
    default-list fallback (a misheard word must never pick a deletion target)."""
    try:
        available = await ha_model.fetch_todo_lists()
    except Exception as exc:
        log.warning("todo list fetch failed: %s", exc)
        return None, _HA_DOWN, []
    if not available:
        return None, _NO_LISTS, []
    ids = {entry["entity_id"] for entry in available}
    wanted = _normalize_list_name(spoken)
    if not wanted:  # a bare "the list" is never enough to delete by
        names = ", ".join(entry["name"] for entry in available)
        return None, f"Which list do you want to delete? You have: {names}.", available
    cur = ha_model.load_curation().get("lists") or {}
    for eid, aliases in (cur.get("aliases") or {}).items():
        if eid in ids and any(_normalize_list_name(str(a)) == wanted for a in aliases):
            return eid, None, available
    for entry in available:
        if _normalize_list_name(entry["name"]) == wanted:
            return entry["entity_id"], None, available
        if entry["entity_id"].split(".", 1)[-1].replace("_", " ") == wanted:
            return entry["entity_id"], None, available
    return None, None, available  # unknown name — miss to the LLM (which can ask)


def _split_items(text: str) -> list[str]:
    """Split a spoken item phrase: "milk, eggs and butter" → three items."""
    parts = re.split(r",\s*|\s+and\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + f" and {items[-1]}"


async def _match_open_items(entity_id: str, wanted: list[str]) -> tuple[list[str], list[str]]:
    """Case-insensitively match wanted names to the exact open-item summaries
    (HA's item services need the exact summary). Returns (matched, missing)."""
    existing = await _get_items(entity_id)
    by_lower = {item.lower(): item for item in existing}
    matched: list[str] = []
    missing: list[str] = []
    for want in wanted:
        hit = by_lower.get(want.lower())
        if hit is None:
            try:
                from rapidfuzz import fuzz, process

                best = process.extractOne(
                    want.lower(), list(by_lower), scorer=fuzz.WRatio, score_cutoff=90
                )
                hit = by_lower[best[0]] if best else None
            except ImportError:  # pragma: no cover
                hit = None
        if hit is not None:
            matched.append(hit)
        else:
            missing.append(want)
    return matched, missing


# ---------------------------------------------------------------------------
# Shared operations (used by both tiers)
# ---------------------------------------------------------------------------


async def _do_add(entity_id: str, name: str, items: list[str]) -> str:
    for item in items:
        await _todo_service("add_item", entity_id, {"item": item})
    return f"Added {_join(items)} to the {name}."


async def _do_read(entity_id: str, name: str) -> str:
    items = await _get_items(entity_id)
    if not items:
        return f"The {name} is empty."
    return f"On the {name}: {_join(items)}."


async def _do_remove(entity_id: str, name: str, items: list[str]) -> str:
    matched, missing = await _match_open_items(entity_id, items)
    for item in matched:
        await _todo_service("remove_item", entity_id, {"item": item})
    parts = []
    if matched:
        parts.append(f"Removed {_join(matched)} from the {name}.")
    if missing:
        parts.append(f"I didn't see {_join(missing)} on it.")
    return " ".join(parts)


async def _do_complete(entity_id: str, name: str, items: list[str]) -> str:
    matched, missing = await _match_open_items(entity_id, items)
    for item in matched:
        await _todo_service("update_item", entity_id, {"item": item, "status": "completed"})
    parts = []
    if matched:
        parts.append(f"Checked off {_join(matched)}.")
    if missing:
        parts.append(f"I didn't see {_join(missing)} on the {name}.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# LLM tools (the fuzzy tier)
# ---------------------------------------------------------------------------


@skill
async def add_to_list(items: list[str], list_name: str = "") -> str:
    """Add one or more items to a shopping/to-do list (kept in Home Assistant).

    Use for "add X to the list", including compound requests you break into
    individual items yourself ("everything for pancakes" → flour, eggs, milk…).

    items: the item names to add, one entry per item
    list_name: which list, as spoken ("shopping", "errands"); empty = the
        household's default list
    """
    if not _ha_configured():
        return _NOT_CONFIGURED
    entity_id, err, available = await _resolve_list(list_name)
    if entity_id is None:
        return err or f"I couldn't find a list called {list_name!r}."
    if not items:
        return "Error: no items given."
    return await _do_add(entity_id, _display(available, entity_id), items)


@skill
async def read_list(list_name: str = "") -> str:
    """Read out what's on a shopping/to-do list (open items only).

    list_name: which list, as spoken; empty = the household's default list
    """
    if not _ha_configured():
        return _NOT_CONFIGURED
    entity_id, err, available = await _resolve_list(list_name)
    if entity_id is None:
        return err or f"I couldn't find a list called {list_name!r}."
    return await _do_read(entity_id, _display(available, entity_id))


@skill
async def remove_from_list(items: list[str], list_name: str = "") -> str:
    """Remove (delete) items from a shopping/to-do list.

    Use for "take X off the list" / "remove X". For marking something done or
    bought while keeping it visible as completed, use complete_list_items.

    items: the item names to remove
    list_name: which list, as spoken; empty = the household's default list
    """
    if not _ha_configured():
        return _NOT_CONFIGURED
    entity_id, err, available = await _resolve_list(list_name)
    if entity_id is None:
        return err or f"I couldn't find a list called {list_name!r}."
    if not items:
        return "Error: no items given."
    return await _do_remove(entity_id, _display(available, entity_id), items)


@skill
async def complete_list_items(items: list[str], list_name: str = "") -> str:
    """Mark items on a shopping/to-do list as done ("check off X", "got the milk").

    items: the item names to check off
    list_name: which list, as spoken; empty = the household's default list
    """
    if not _ha_configured():
        return _NOT_CONFIGURED
    entity_id, err, available = await _resolve_list(list_name)
    if entity_id is None:
        return err or f"I couldn't find a list called {list_name!r}."
    if not items:
        return "Error: no items given."
    return await _do_complete(entity_id, _display(available, entity_id), items)


@skill
async def create_list(name: str, items: list[str] | None = None) -> str:
    """Create a new to-do list in Home Assistant (a Local to-do list).

    Use ONLY when the user explicitly asks for a new list ("create a camping
    list") or has just confirmed they want one created — never create a list
    implicitly. Optionally add initial items in the same step.

    name: the list's display name, e.g. "Shopping list" or "Camping list"
    items: optional item names to add right after creating
    """
    if not _ha_configured():
        return _NOT_CONFIGURED
    name = name.strip()
    if not name:
        return "Error: the list needs a name."
    return await _create_and_add(name, list(items or []))


@skill
async def delete_list(name: str) -> str:
    """Delete an ENTIRE to-do list from Home Assistant (not items on it — use
    remove_from_list for items). This asks the user for spoken confirmation
    itself and reports the outcome — deletion only ever happens on their yes.
    Only locally-stored lists can be deleted; lists synced from outside
    services can't be.

    name: the exact list name as the user said it, e.g. "grocery list"
    """
    if not _ha_configured():
        return _NOT_CONFIGURED
    entity_id, err, available = await _resolve_list_strict(name)
    if entity_id is None:
        if err:
            return err
        names = ", ".join(entry["name"] for entry in available) or "none"
        return f"Error: no list matches {name!r} exactly. The lists are: {names}."
    return await _confirm_delete(entity_id, _display(available, entity_id))


# ---------------------------------------------------------------------------
# Fast intents (the instant tier)
# ---------------------------------------------------------------------------

_ADD_RE = re.compile(r"^(?:add|put) (?P<items>.+?) (?:to|on|onto) (?P<list>.+)$")
_READ_RE = re.compile(r"^(?:what(?:'s| is) on|read(?: me)?|read out) (?P<list>.+)$")
_REMOVE_RE = re.compile(r"^(?:remove|take|delete) (?P<items>.+?) (?:off|from) (?P<list>.+)$")
# "off" (or an explicit "mark … done") is required — a bare "check X" is a
# status question ("check the weather"), not a list operation.
_COMPLETE_RE = re.compile(
    r"^(?:check|tick|cross) off (?P<items>.+?)(?: (?:on|from) (?P<list>.+))?$"
    r"|^(?:check|tick|cross) (?P<items2>.+?) off(?: (?:on|from) (?P<list2>.+))?$"
    r"|^mark (?P<items3>.+?) (?:as )?(?:done|complete|completed|bought)"
    r"(?: (?:on|from) (?P<list3>.+))?$"
)
# Whole-list deletion. Item removal ("delete milk FROM the list") is _REMOVE_RE —
# its required off/from keeps the two apart. The strict resolver is the miss gate:
# "delete broccoli" resolves to no list and falls through to the LLM.
_DELETE_LIST_RE = re.compile(r"^(?:delete|get rid of|erase|throw away|trash) (?P<list>.+)$")

_VOICE = "Speak briefly and clearly, like a helpful assistant confirming a request."

# Confirmations ride the 4.2 ask() primitive: the skill parks on the spoken
# question and resumes with the answer — the server routes the room's next
# utterance back here, and the wake word always cancels (reply None). Nothing
# is ever created or deleted without the spoken yes, exactly as before; the
# per-room TTL'd pending dicts this replaces are gone.
_YES = frozenset(
    {
        "yes",
        "yeah",
        "yep",
        "yup",
        "sure",
        "okay",
        "ok",
        "please",
        "please do",
        "go ahead",
        "do it",
        "yes please",
        "sounds good",
    }  # fmt: skip
)
_NO = frozenset(
    {
        "no",
        "nope",
        "no thanks",
        "no thank you",
        "don't",
        "do not",
        "cancel",
        "never mind",
        "nevermind",
    }  # fmt: skip
)


def _is_yes(answer: str | None) -> bool:
    return answer is not None and _normalize(answer) in _YES


def _is_no(answer: str | None) -> bool:
    return answer is not None and _normalize(answer) in _NO



def _proposed_name(spoken_list: str) -> str:
    """A sensible name for the list we offer to create ("the shopping list" →
    "Shopping list"; a bare "the list" → "Shopping list")."""
    wanted = _normalize_list_name(spoken_list)
    return f"{wanted.title()} list" if wanted else "Shopping list"


# With zero lists (or HA down) there's nothing to resolve against, so name
# resolution can't be the miss gate — this is: only phrases that plainly refer
# to a list get the no-lists/HA-down replies or a create offer. "What's on tv" /
# "add it to my favorites" must still miss to the LLM.
_LISTY_RE = re.compile(r"\b(?:lists?|to.?do|shopping|grocery|groceries)\b")


def _looks_like_list(spoken: str) -> bool:
    return not _normalize_list_name(spoken) or bool(_LISTY_RE.search(spoken.lower()))


_NOT_DELETABLE = (
    "That list is managed by an outside service, not by Home Assistant itself, "
    "so I can't delete it — remove it from that service instead."
)
_DELETE_FAILED = (
    "I couldn't delete it — you can remove it in Home Assistant under "
    "Settings, then Devices and Services."
)


async def _delete_question(entity_id: str, name: str) -> tuple[str, str | None]:
    """Verify the list is ours to delete; return (question-or-refusal, entry_id).
    entry_id None ⇒ refusal (synced provider) — nothing to confirm."""
    entry_id = await _local_entry_id(entity_id)
    if entry_id is None:
        return _NOT_DELETABLE, None
    count = len(await _get_items(entity_id))
    if count:
        things = "1 item" if count == 1 else f"{count} items"
        return f"The {name} still has {things} on it. Delete it for good?", entry_id
    return f"Delete the {name}? It's empty.", entry_id


async def _confirm_delete(entity_id: str, name: str) -> str:
    """The whole confirmed-deletion conversation, on ask(): question → spoken
    yes → delete. A no (or anything that isn't a yes) keeps the list; a wake
    cancel abandons silently (the reply is discarded by design)."""
    question, entry_id = await _delete_question(entity_id, name)
    if entry_id is None:
        return question  # refusal — no confirmation loop
    answer = await ask(question)
    if answer is None:
        return "Okay."  # canceled — discarded upstream
    if _is_yes(answer):
        ok = await _delete_entry(entry_id)
        return f"Deleted the {name}." if ok else _DELETE_FAILED
    return f"Okay, keeping the {name}."


def _fallthrough(err: str | None, list_text: str) -> FastResult:
    """Common miss/handled/clarify logic when a list didn't resolve."""
    if err in (_HA_DOWN, _NO_LISTS):
        if _looks_like_list(list_text):
            return FastResult.handled(err, _VOICE)
        return FastResult.miss()  # "what's on tv" — not about lists at all
    if err:  # ambiguous default — ask, mic re-opens
        return FastResult.clarify(err)
    return FastResult.miss()  # unknown list name ("add it to my favorites")


def _normalize(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[!?.]+$", "", text).strip()
    text = re.sub(r"^(?:please|hey|ok|okay)[, ]+", "", text)
    text = re.sub(r"[, ]+please$", "", text)
    return re.sub(r"\s+", " ", text)


@fast_intent(priority=93)
async def fast_lists(utterance: str, room_id: str | None, speaker: str | None) -> FastResult:
    """Deterministic add/read/remove/check-off for HA to-do lists.

    The list-name resolution is the gate: "add this to my calendar" or
    "what's on tv" won't resolve to a todo entity, so they miss to the LLM.
    """
    if not _ha_configured():
        return FastResult.miss()  # lists are an HA feature; without HA, stay out
    text = _normalize(utterance)

    m = _ADD_RE.match(text)
    if m:
        entity_id, err, available = await _resolve_list(m.group("list"))
        if entity_id is None:
            if err == _NO_LISTS and _looks_like_list(m.group("list")):
                # Offer to create one (confirmed, never implicit — a misheard
                # command must not conjure infrastructure in HA). ask() parks
                # here; the room's spoken answer resumes us.
                name = _proposed_name(m.group("list"))
                answer = await ask(
                    "You don't have any to-do lists in Home Assistant yet. "
                    f"Should I create one called {name}?"
                )
                if answer is None:
                    return FastResult.handled("Okay.", _VOICE)  # canceled — discarded
                if _is_yes(answer):
                    return FastResult.handled(
                        await _create_and_add(name, _split_items(m.group("items"))), _VOICE
                    )
                return FastResult.handled("Okay, I won't create one.", _VOICE)
            return _fallthrough(err, m.group("list"))
        reply = await _do_add(
            entity_id, _display(available, entity_id), _split_items(m.group("items"))
        )
        return FastResult.handled(reply, _VOICE)

    m = _READ_RE.match(text)
    if m:
        entity_id, err, available = await _resolve_list(m.group("list"))
        if entity_id is None:
            return _fallthrough(err, m.group("list"))
        return FastResult.handled(await _do_read(entity_id, _display(available, entity_id)), _VOICE)

    m = _REMOVE_RE.match(text)
    if m:
        entity_id, err, available = await _resolve_list(m.group("list"))
        if entity_id is None:
            return _fallthrough(err, m.group("list"))
        reply = await _do_remove(
            entity_id, _display(available, entity_id), _split_items(m.group("items"))
        )
        return FastResult.handled(reply, _VOICE)

    m = _COMPLETE_RE.match(text)
    if m:
        items_text = m.group("items") or m.group("items2") or m.group("items3") or ""
        list_text = m.group("list") or m.group("list2") or m.group("list3") or ""
        entity_id, err, available = await _resolve_list(list_text)
        if entity_id is None:
            # A bare "check off milk" is list-generic; an explicit list is checked.
            return _fallthrough(err, list_text or "the list")
        reply = await _do_complete(
            entity_id, _display(available, entity_id), _split_items(items_text)
        )
        return FastResult.handled(reply, _VOICE)

    m = _DELETE_LIST_RE.match(text)
    if m:
        # Destructive: strict resolution (exact/alias only, no fuzzy, no default),
        # local_todo only, and never without the spoken confirmation that follows.
        entity_id, err, available = await _resolve_list_strict(m.group("list"))
        if entity_id is None:
            if err in (_HA_DOWN, _NO_LISTS):
                return _fallthrough(err, m.group("list"))
            if err:  # bare "delete the list" — ask which; nothing is staged
                return FastResult.clarify(err)
            return FastResult.miss()  # unknown name ("delete broccoli") → LLM
        return FastResult.handled(
            await _confirm_delete(entity_id, _display(available, entity_id)), _VOICE
        )

    return FastResult.miss()
