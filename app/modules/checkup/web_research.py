"""Bounded web research for report questions: one search, at most two page reads.

The budget is counted in HTTP attempts (Search + Fetch + retries) and is owned by
the caller (``answer_report``). This module never decides *whether* to search; it
only executes a query and reports truthfully what happened, including failures.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.schemas.checkup import ExternalSource

MAX_ATTEMPTS = 6
MAX_SOURCES = 5
MAX_PAGES = 2
MAX_PAGE_CHARS = 3000
MAX_TOTAL_PAGE_CHARS = 6000


@dataclass
class WebPage:
    url: str
    title: str
    content: str


@dataclass
class ResearchResult:
    status: str  # "searched" | "no_results" | "failed"
    query: str
    error: str | None = None
    sources: list[ExternalSource] = field(default_factory=list)
    pages: list[WebPage] = field(default_factory=list)
    attempts: int = 0
    elapsed_ms: int = 0


def _source_of(item: dict[str, Any]) -> ExternalSource | None:
    url = str(item.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    title = str(item.get("title") or item.get("site_name") or url).strip()[:300] or url[:300]
    snippet = str(item.get("summary") or item.get("snippet") or "").strip()[:1000]
    date = item.get("date")
    site_name = item.get("site_name")
    authority = item.get("authority")
    return ExternalSource(
        title=title,
        url=url[:2000],
        published_at=str(date) if date else None,
        snippet=snippet or None,
        site_name=str(site_name) if site_name else None,
        authority=authority if isinstance(authority, int) and not isinstance(authority, bool) else None,
    )


def _rank(item: dict[str, Any]) -> tuple[float, float]:
    """Prefer authoritative and well-ranked results when choosing pages to read."""
    authority = item.get("authority")
    score = item.get("rerank_score")
    authority_value = float(authority) if isinstance(authority, (int, float)) and not isinstance(authority, bool) else -1.0
    score_value = float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else 0.0
    return authority_value, score_value


def _call_search(search, query: str, question: str | None) -> list[dict[str, Any]]:
    # Older/simple search providers (and test doubles) may not accept ori_question.
    try:
        return search.search(query, count=MAX_SOURCES, need_content=False, ori_question=question)
    except TypeError:
        return search.search(query, count=MAX_SOURCES, need_content=False)


def research_web(
    search,
    query: str,
    *,
    question: str | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> ResearchResult:
    """Run one bounded search and read at most a couple of pages of content."""
    started = time.monotonic()
    attempts = 0
    max_attempts = max(1, int(max_attempts))

    def finish(status: str, sources: list[ExternalSource], pages: list[WebPage], error: str | None) -> ResearchResult:
        return ResearchResult(
            status=status, query=query, error=error, sources=sources, pages=pages,
            attempts=attempts, elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    try:
        attempts += 1
        raw_results = _call_search(search, query, question)
    except Exception as exc:  # provider and test doubles raise plain exceptions
        message = str(exc).strip() or "联网检索失败。"
        return finish("failed", [], [], message[:500])

    items = [item for item in raw_results if isinstance(item, dict)]
    sources: list[ExternalSource] = []
    seen_urls: set[str] = set()
    for item in items:
        source = _source_of(item)
        if source is None or source.url in seen_urls:
            continue
        seen_urls.add(source.url)
        sources.append(source)
        if len(sources) >= MAX_SOURCES:
            break
    if not sources:
        return finish("no_results", [], [], None)

    pages: list[WebPage] = []
    fetch = getattr(search, "fetch", None)
    if callable(fetch):
        ranked = sorted(items, key=_rank, reverse=True)
        for item in ranked:
            if len(pages) >= MAX_PAGES or attempts >= max_attempts:
                break
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            try:
                attempts += 1
                page = fetch(url)
            except Exception:
                # A failed page read does not invalidate the search results; the
                # model can still use the summaries and the failure stays local.
                continue
            content = str(page.get("content") or "").strip()
            if not content:
                continue
            pages.append(WebPage(
                url=url,
                title=str(page.get("title") or item.get("title") or url)[:300],
                content=content[:MAX_PAGE_CHARS],
            ))
            if sum(len(page.content) for page in pages) >= MAX_TOTAL_PAGE_CHARS:
                break
    return finish("searched", sources, pages, None)
