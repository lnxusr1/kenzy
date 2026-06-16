"""
News skill for kenzy-llm — headlines and article summaries via RSS.

Two-step flow:
  1. get_news(category)              → numbered headline list, short enough for TTS
  2. get_news_article(category, N)   → fetches article N, summarises via sub-LLM

Config in llm.yaml under skills.news:
  max_headlines: 5
  model:    "gpt-4o"    # model for article summarisation (defaults to gpt-4o)
  base_url: null        # only needed for local providers
  feeds:
    latest:   "https://..."
    local:    "https://..."
    world:    "https://..."
    politics: "https://..."
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import urlparse

import feedparser  # type: ignore[import-untyped]
import httpx

from kenzy.llm.skills import get_config, skill  # type: ignore[import]

log = logging.getLogger(__name__)

_DEFAULT_FEEDS: dict[str, str] = {
    "local":    "https://myfox8.com/feed/",
    "latest":   "https://moxie.foxnews.com/google-publisher/latest.xml",
    "world":    "https://moxie.foxnews.com/google-publisher/world.xml",
    "politics": "https://moxie.foxnews.com/google-publisher/politics.xml",
}

_DEFAULT_MAX_HEADLINES = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _root_domain(url: str) -> str:
    host = urlparse(url).netloc.lstrip("www.")
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _is_ad(entry: object, feed_domain: str) -> bool:
    title = _strip_html(getattr(entry, "title", "") or "")
    link  = getattr(entry, "link", "") or ""
    if len(title) < 6:
        return True
    if link:
        entry_domain = _root_domain(link)
        if entry_domain and entry_domain != feed_domain:
            return True
    return False


async def _fetch_entries(category: str) -> list[Any]:
    """Fetch RSS feed for category and return filtered entries."""
    feeds: dict[str, str] = get_config("news", "feeds") or _DEFAULT_FEEDS
    url = feeds.get(category.lower()) or feeds.get("latest") or next(iter(feeds.values()))
    feed_domain = _root_domain(url)

    async with httpx.AsyncClient(
        timeout=15,
        follow_redirects=True,
        headers={"User-Agent": "kenzy-news/1.0"},
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        raw = resp.content

    feed = feedparser.parse(raw)
    return [e for e in feed.entries if not _is_ad(e, feed_domain)]


def _extract_article_text(url: str) -> str:
    """Synchronous: fetch and extract main article body via trafilatura."""
    try:
        import trafilatura  # type: ignore[import-untyped]
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            text = trafilatura.extract(
                downloaded,
                include_comments=False,
                include_tables=False,
                no_fallback=False,
            )
            if text:
                return text
    except Exception as exc:
        log.debug("trafilatura failed for %s: %s", url, exc)
    return ""


async def _summarize(title: str, body: str) -> str:
    """Sub-LLM: produce a 2–3 sentence spoken summary of an article."""
    from litellm import acompletion  # type: ignore[import-untyped]

    model    = get_config("news", "model")    or "gpt-4o"
    base_url = get_config("news", "base_url") or None

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are summarising a news article for a voice assistant. "
                    "Write a thorough spoken summary: cover the key facts, "
                    "context, and any notable details or quotes from the article. "
                    "Aim for 5–8 sentences — enough that the listener gets a real "
                    "sense of the story without needing to read it themselves. "
                    "Write in plain, conversational prose suitable for reading "
                    "aloud.  No bullet points, no markdown, no source attribution, "
                    "no phrases like 'the article says' or 'according to'."
                ),
            },
            {
                "role": "user",
                "content": f"Title: {title}\n\n{body[:4000]}",
            },
        ],
    }
    if base_url:
        kwargs["base_url"] = base_url

    response = await acompletion(**kwargs)
    return (response.choices[0].message.content or "").strip()


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

@skill
async def get_news(category: str = "latest") -> str:
    """Fetch the latest news headlines for a given category.

    Use when the user asks for news, headlines, or what's happening — e.g.
    "what's in the news?", "give me the latest headlines", "any local news?",
    "what's happening in politics?", "world news please".

    Returns a short numbered list of titles only.  After reading the list,
    always ask the user if they would like more details on any story.  They
    can then request a specific article by number via get_news_article.

    category: news category — one of the keys configured under
              skills.news.feeds in llm.yaml (latest, local, world, politics).
              Defaults to "latest" if not specified or unrecognised.
    """
    feeds: dict[str, str] = get_config("news", "feeds") or _DEFAULT_FEEDS
    max_items: int = int(get_config("news", "max_headlines") or _DEFAULT_MAX_HEADLINES)

    try:
        entries = await _fetch_entries(category)
    except Exception as exc:
        return f"Could not fetch the news feed: {exc}"

    if not entries:
        return "No news headlines available right now."

    label = category.capitalize() if category.lower() in feeds else "Latest"
    lines: list[str] = [f"{label} news:"]
    for i, entry in enumerate(entries[:max_items], start=1):
        title = _strip_html(getattr(entry, "title", "") or "").strip()
        if title:
            lines.append(f"{i}. {title}")

    if len(lines) == 1:
        return "No readable headlines found."

    return "\n".join(lines)


@skill
async def get_news_article(category: str = "latest", article_number: int = 1) -> str:
    """Fetch and summarise a specific news article by its number.

    Use after get_news when the user asks for more details on a story —
    e.g. "tell me more about number 2", "what's the story on the third one?",
    "give me details on the first article".

    category:       the same category passed to get_news.
    article_number: 1-based position of the article in the headlines list.
    """
    max_items: int = int(get_config("news", "max_headlines") or _DEFAULT_MAX_HEADLINES)

    try:
        entries = await _fetch_entries(category)
    except Exception as exc:
        return f"Could not fetch the news feed: {exc}"

    idx = article_number - 1
    if idx < 0 or idx >= len(entries[:max_items]):
        return f"I don't have an article number {article_number} in that list."

    entry = entries[idx]
    title = _strip_html(getattr(entry, "title", "") or "").strip()
    link  = getattr(entry, "link", "") or ""

    # Prefer full article text from the page; fall back to RSS summary/content.
    body = ""
    if link:
        loop = asyncio.get_running_loop()
        body = await loop.run_in_executor(None, _extract_article_text, link)

    if not body:
        rss_content = ""
        if hasattr(entry, "content") and entry.content:
            rss_content = entry.content[0].get("value", "")
        body = _strip_html(
            rss_content or getattr(entry, "summary", "") or ""
        ).strip()

    if not body:
        return f"I couldn't retrieve the content for \"{title}\"."

    try:
        return await _summarize(title, body)
    except Exception as exc:
        log.error("Article summarisation failed: %s", exc, exc_info=True)
        # Graceful fallback: first two sentences of raw body.
        sentences = re.split(r"(?<=[.!?])\s+", body)
        return " ".join(sentences[:2])
