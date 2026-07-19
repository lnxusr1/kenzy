"""Web search skill: provider dispatch + result formatting (network mocked)."""

from __future__ import annotations

import pytest

from kenzy.llm import skills as sk
from kenzy.llm.builtin_skills import web_search

_FAKE = [
    {"title": "Mount Everest", "href": "https://en.wikipedia.org/wiki/Everest",
     "body": "  8,849 m   tall.  "},
    {"title": "K2", "href": "https://www.example.com/k2", "body": "Second highest."},
]


@pytest.fixture(autouse=True)
def _clean_config():
    sk.set_config({})
    yield
    sk.set_config({})


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def test_format_numbers_results_with_source_and_trimmed_snippet():
    out = web_search._format("tallest mountain", _FAKE)
    assert 'Web search results for "tallest mountain":' in out
    assert "1. Mount Everest (source: en.wikipedia.org)" in out  # www. stripped, source shown
    assert "8,849 m tall." in out  # collapsed whitespace
    assert "2. K2 (source: example.com)" in out


def test_format_empty_is_graceful():
    assert web_search._format("nothing", []) == 'No web results found for "nothing".'


def test_source_strips_www():
    assert web_search._source("https://www.foo.com/bar") == "foo.com"
    assert web_search._source("https://news.bar.org/x") == "news.bar.org"


# ---------------------------------------------------------------------------
# Provider dispatch
# ---------------------------------------------------------------------------


async def test_default_provider_is_duckduckgo(monkeypatch):
    seen = {}

    def fake_ddg(query, max_results, timeout):  # noqa: ANN001, ANN202
        seen["query"] = query
        seen["max_results"] = max_results
        return _FAKE

    monkeypatch.setattr(web_search, "_search_duckduckgo", fake_ddg)
    out = await web_search.web_search("tallest mountain")
    assert seen == {"query": "tallest mountain", "max_results": 5}  # default max_results
    assert "Mount Everest" in out


async def test_searxng_selected_by_config(monkeypatch):
    sk.set_config({"web_search": {"provider": "searxng", "max_results": 2}})
    called = {}

    async def fake_searxng(query, max_results, timeout):  # noqa: ANN001, ANN202
        called["query"] = query
        called["max_results"] = max_results
        return _FAKE

    monkeypatch.setattr(web_search, "_search_searxng", fake_searxng)
    out = await web_search.web_search("k2 height")
    assert called == {"query": "k2 height", "max_results": 2}
    assert "K2" in out


async def test_missing_ddgs_returns_install_hint(monkeypatch):
    def raise_import(query, max_results, timeout):  # noqa: ANN001, ANN202
        raise ImportError("no ddgs")

    monkeypatch.setattr(web_search, "_search_duckduckgo", raise_import)
    out = await web_search.web_search("anything")
    assert "ddgs" in out and "pip install" in out


async def test_provider_error_is_reported_not_raised(monkeypatch):
    async def boom(query, max_results, timeout):  # noqa: ANN001, ANN202
        raise RuntimeError("network down")

    sk.set_config({"web_search": {"provider": "searxng"}})
    monkeypatch.setattr(web_search, "_search_searxng", boom)
    out = await web_search.web_search("q")
    assert "couldn't complete the web search" in out.lower()


def test_skill_is_registered():
    tools = {t["function"]["name"] for t in sk.get_tools()}
    assert "web_search" in tools
