"""
Web search skill for kenzy-llm.

Gives the LLM a general web-search tool for questions that need current, live, or
niche information the model can't answer from its own knowledge (recent events,
prices, "look up …", "search the web for …"). The main LLM is already in a
tool-calling loop, so this skill just returns the top results (title + snippet +
source) as text and lets the model synthesise the spoken answer.

Two providers, selected by ``skills.web_search.provider``:
  * ``duckduckgo`` (default) — keyless, zero setup, via the ``ddgs`` package.
  * ``searxng`` — a self-hosted SearXNG instance (nothing leaves your network);
    point ``skills.web_search.searxng_url`` at its ``/search`` endpoint, which
    must have the JSON output format enabled (``search.formats: [html, json]``
    in SearXNG's ``settings.yml``).

Config in llm.yaml under skills.web_search:
  provider:     "duckduckgo"      # or "searxng"
  max_results:  5
  timeout:      15
  region:       "wt-wt"           # duckduckgo region (wt-wt = no region)
  searxng_url:  "http://localhost:8888/search"
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from kenzy.llm.skills import get_config, skill  # type: ignore[import]

log = logging.getLogger(__name__)

_DEFAULT_MAX_RESULTS = 5
_DEFAULT_TIMEOUT = 15.0
_DEFAULT_SEARXNG_URL = "http://localhost:8888/search"
_SNIPPET_CHARS = 300


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source(url: str) -> str:
    """Short source label from a result URL (``example.com``)."""
    host = urlparse(url).netloc
    return host[4:] if host.startswith("www.") else host


def _format(query: str, results: list[dict[str, str]]) -> str:
    """Render results as a compact numbered list for the LLM to read and cite."""
    if not results:
        return f'No web results found for "{query}".'
    lines = [f'Web search results for "{query}":', ""]
    for i, r in enumerate(results, start=1):
        title = (r.get("title") or "").strip()
        snippet = " ".join((r.get("body") or "").split())[:_SNIPPET_CHARS]
        src = _source(r.get("href") or "")
        head = f"{i}. {title}" + (f" (source: {src})" if src else "")
        lines.append(head)
        if snippet:
            lines.append(f"   {snippet}")
    return "\n".join(lines)


def _cfg_int(key: str, default: int) -> int:
    try:
        return int(get_config("web_search", key, default))
    except (TypeError, ValueError):
        return default


def _cfg_float(key: str, default: float) -> float:
    try:
        return float(get_config("web_search", key, default))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


def _search_duckduckgo(query: str, max_results: int, timeout: float) -> list[dict[str, str]]:
    """Keyless DuckDuckGo search (synchronous — run in an executor)."""
    from ddgs import DDGS  # lazy: only needed for this provider

    region = str(get_config("web_search", "region", "wt-wt") or "wt-wt")
    with DDGS(timeout=timeout) as ddgs:
        raw = ddgs.text(query, region=region, max_results=max_results) or []
    # ddgs already returns title / href / body.
    return [
        {"title": r.get("title", ""), "href": r.get("href", ""), "body": r.get("body", "")}
        for r in raw
    ]


async def _search_searxng(query: str, max_results: int, timeout: float) -> list[dict[str, str]]:
    """Query a self-hosted SearXNG instance (JSON output format must be enabled)."""
    url = str(get_config("web_search", "searxng_url", _DEFAULT_SEARXNG_URL) or _DEFAULT_SEARXNG_URL)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(
            url,
            params={"q": query, "format": "json"},
            headers={"User-Agent": "kenzy-web-search/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
    results = data.get("results", []) if isinstance(data, dict) else []
    out: list[dict[str, str]] = []
    for r in results[:max_results]:
        out.append(
            {
                "title": r.get("title", ""),
                "href": r.get("url", ""),
                "body": r.get("content", ""),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------


@skill
async def web_search(query: str) -> str:
    """Search the web for current, live, or factual information.

    Use this whenever answering needs information you don't reliably know —
    recent events or news, today's facts, prices, sports scores, opening hours,
    product or technical details, or any "look it up / search the web for …"
    request. Prefer this over guessing when the answer may have changed since
    your training or is too specific to recall.

    Returns a short numbered list of result titles, snippets, and sources; read
    it and synthesise a concise spoken answer for the user.

    query: the search query — a natural-language question or keywords.
    """
    provider = str(get_config("web_search", "provider", "duckduckgo") or "duckduckgo").lower()
    max_results = _cfg_int("max_results", _DEFAULT_MAX_RESULTS)
    timeout = _cfg_float("timeout", _DEFAULT_TIMEOUT)

    try:
        if provider == "searxng":
            results = await _search_searxng(query, max_results, timeout)
        else:
            if provider != "duckduckgo":
                log.warning("Unknown web_search provider %r; using duckduckgo", provider)
            loop = asyncio.get_running_loop()
            results = await loop.run_in_executor(
                None, _search_duckduckgo, query, max_results, timeout
            )
    except ImportError:
        return (
            "Web search needs the 'ddgs' package, which isn't installed. "
            "Install it with: pip install ddgs"
        )
    except Exception as exc:
        log.error("Web search (%s) failed: %s", provider, exc, exc_info=True)
        return f"I couldn't complete the web search: {exc}"

    return _format(query, results)
