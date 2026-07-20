"""Voice enrollment — the whole conversation, on ``ask_audio()`` (4.2).

This skill IS the enrollment flow now: it reads the prompt sentences from the
speaker service (``GET /enroll/info`` — single source of truth with
``kenzy-enroll`` and the dashboard's Services editor), speaks each prompt via
``ask_audio()`` (record-after-the-tone: the node chimes when the window
opens), receives the RAW captured sample back, and POSTs it to the speaker
service's ``/enroll``. Person-first bookkeeping rides an ``adopt_voice``
action on the first stored sample — people.yaml stays server-owned.

Three entries, one driver:

- **Voice** ("enroll me as Alice") — the LLM calls the ``enroll_speaker``
  tool; gated by the speaker service's ``allow_voice_enroll`` (earshot
  opt-in, off by default).
- **Dashboard** (People → Enroll voice) — the server sends the internal
  ``[[enroll]] …`` directive through the pipeline; a fast intent matches it
  exactly. ``operator=1`` bypasses the earshot gate (the request was already
  authenticated and controls-gated). STT can't produce ``[[``, so spoken
  audio can never forge the directive.
- The ``kenzy-enroll`` CLI keeps its direct service path, untouched.

The old server-side session state machine (prompt loops, retry counters,
inactivity timers) is gone — ask_audio()'s park/resume covers all of it: the
wake word cancels (ask returns None), an expired reply window arrives as an
EMPTY sample (the retry path), and the attempt cap bounds the loop.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from kenzy.llm.skills import (  # type: ignore[import]
    FastResult,
    add_action,
    ask_audio,
    fast_intent,
    get_config,
    get_request,
    request_channel,
    skill,
)

log = logging.getLogger(__name__)

#: ~0.5 s of 16 kHz int16 — shorter captures are retried (mirrors the old
#: server-side gate).
_MIN_PCM_BYTES = 16000
#: Extra tries beyond one-per-prompt before giving up.
_MAX_RETRIES = 4


def _slug(name: str) -> str:
    # Deliberately duplicated from kenzy.server.people.slugify (wire contract —
    # this service never imports the server package): profile name == the id
    # the person record will get, so renames never touch the .npy file.
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "person"


def _speaker_base() -> str | None:
    """The speaker service base URL. Per-request server injection first (it
    resolves static config ← auto-registered heartbeat, so it works on every
    topology); the config-injected peer URL (_SERVICE_PEERS) is the fallback
    for direct/offline invocations."""
    injected = get_request("speaker_url")
    if injected:
        return str(injected).rstrip("/")
    url = get_config("speaker", "url")
    if not url:
        return None
    return str(url).rsplit("/", 1)[0]


def _auth_headers(method: str, url: str) -> dict[str, str]:
    from urllib.parse import urlparse

    from kenzy.serviceauth import service_token_from_env, sign_service_request

    token = service_token_from_env()
    if not token:
        return {}
    path = urlparse(url).path or "/"
    return {"X-Kenzy-Auth": sign_service_request(token, method, path)}


async def _enroll_info(base: str) -> dict[str, Any] | None:
    import httpx  # type: ignore[import-untyped]

    from kenzy import tlsutil

    url = f"{base}/enroll/info"
    try:
        async with httpx.AsyncClient(verify=tlsutil.httpx_verify()) as client:
            r = await client.get(url, timeout=10.0, headers=_auth_headers("GET", url))
            r.raise_for_status()
            return dict(r.json())
    except Exception as exc:
        log.warning("enroll: speaker /enroll/info unreachable: %s", exc)
        return None


async def _post_sample(base: str, voiceprint: str, pcm: bytes) -> bool:
    import base64

    import httpx  # type: ignore[import-untyped]

    from kenzy import tlsutil

    url = f"{base}/enroll"
    try:
        async with httpx.AsyncClient(verify=tlsutil.httpx_verify()) as client:
            r = await client.post(
                url,
                json={"name": voiceprint, "audio_b64": base64.b64encode(pcm).decode()},
                timeout=30.0,
                headers=_auth_headers("POST", url),
            )
            r.raise_for_status()
            return True
    except Exception as exc:
        log.warning("enroll: /enroll sample failed: %s", exc)
        return False


def _resolve_person(
    name: str, person_id: str | None, people: list[dict[str, Any]]
) -> tuple[str, str, str | None] | None:
    """Person-first profile keying → (voiceprint, display, person_id|None).

    An existing person's voice appends to their profile (more samples); a
    voiceless person gets a fresh profile keyed by their stable id; an unknown
    name gets the slug their person record will receive on adoption — so the
    profile name never drifts from the person."""
    if person_id:
        p = next((x for x in people if x.get("id") == person_id), None)
        if p is None:
            return None
        vps = list(p.get("voiceprints") or [])
        return (vps[0] if vps else str(p["id"])), str(p.get("name") or p["id"]), str(p["id"])
    low = name.lower()
    p = next(
        (x for x in people if any(str(v).lower() == low for v in x.get("voiceprints") or [])),
        None,
    ) or next((x for x in people if str(x.get("name", "")).lower() == low), None)
    if p is not None:
        vps = list(p.get("voiceprints") or [])
        existing = next((str(v) for v in vps if str(v).lower() == low), None)
        voiceprint = existing or (vps[0] if vps else str(p["id"]))
        return voiceprint, str(p.get("name") or p["id"]), str(p["id"])
    return _slug(name), name, None


async def _run_enrollment(name: str, *, operator: bool, person_id: str | None = None) -> str:
    base = _speaker_base()
    if base is None:
        return "Speaker identification isn't set up, so I can't enroll."
    info = await _enroll_info(base)
    if info is None:
        return "The speaker service isn't reachable right now, so I can't enroll."
    if not operator and not info.get("allow_voice_enroll", False):
        return "Voice enrollment is turned off."
    name = name.strip()
    if not name and not person_id:
        return "I didn't catch the name to enroll."
    resolved = _resolve_person(name, person_id, list(get_request("people") or []))
    if resolved is None:
        return "I couldn't find that person to enroll."
    voiceprint, display, pid = resolved
    prompts = [str(p).strip() for p in info.get("prompts") or [] if str(p).strip()]
    if not prompts:
        return "No enrollment prompts are configured."

    collected = attempts = 0
    prompt = f"Okay, enrolling {display}. After the tone, please say: {prompts[0]}"
    while collected < len(prompts):
        if attempts >= len(prompts) + _MAX_RETRIES:
            return "I couldn't get enough clear audio. Enrollment cancelled."
        pcm = await ask_audio(prompt)
        if pcm is None:
            # Wake word / restart: the conversation is over (this text is
            # discarded upstream — never actuate anything after a None).
            return "Enrollment cancelled."
        attempts += 1
        ok = len(pcm) >= _MIN_PCM_BYTES and await _post_sample(base, voiceprint, pcm)
        if ok:
            collected += 1
            if collected == 1:
                # Person-first invariant, enforced on the FIRST stored sample
                # so even an interrupted enrollment never orphans a voice.
                # Actions ride the next reply — the server links people.yaml.
                add_action(
                    {
                        "type": "adopt_voice",
                        "voiceprint": voiceprint,
                        "display": display,
                        "person_id": pid,
                    }
                )
        if collected >= len(prompts):
            return f"All done — I've enrolled {display}."
        sentence = prompts[collected]
        prompt = (
            f"Got it. Next, please say: {sentence}"
            if ok
            else f"I didn't catch that. Please say: {sentence}"
        )
    return f"All done — I've enrolled {display}."


@skill
async def enroll_speaker(name: str) -> str:
    """Enroll (register) a person's voice so Kenzy can recognize them later.

    Use when the user asks to be remembered or enrolled — e.g. "enroll me as
    Alice", "remember my voice as Bob". This skill runs the whole enrollment
    conversation itself (prompts, samples, confirmation) — report its return
    value to the user as the outcome.

    :param name: The name to enroll the speaker under.
    """
    name = name.strip()
    if not name:
        return "I need a name to enroll the voice under — ask the user who this is."
    if request_channel() != "voice":  # F3: enrollment records at a room's mic
        return (
            "Voice enrollment happens at a room speaker — ask from a room device, "
            "or start it from the dashboard's People page."
        )
    return await _run_enrollment(name, operator=False)


_DIRECTIVE_RE = re.compile(
    r"^\[\[enroll\]\] operator=(?P<op>[01]) person=(?P<pid>\S*) name=(?P<name>.*)$"
)


@fast_intent(priority=99)
async def fast_enroll_directive(
    utterance: str, room_id: str | None, speaker: str | None
) -> FastResult:
    """The dashboard's internal entry (server-sent directive; STT can never
    produce '[[', so spoken audio can't forge operator mode)."""
    m = _DIRECTIVE_RE.match(utterance.strip())
    if m is None:
        return FastResult.miss()
    if request_channel() != "voice":
        # Defense-in-depth: the Assist channel carries TYPED text, so the
        # directive is forgeable there (and enrollment needs a room mic anyway).
        return FastResult.miss()
    text = await _run_enrollment(
        m.group("name").strip(),
        operator=m.group("op") == "1",
        person_id=m.group("pid") or None,
    )
    return FastResult.handled(text)
