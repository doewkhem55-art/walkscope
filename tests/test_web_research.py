"""Bounded Search/Fetch research: attempt budget, source limits, honest failures."""

import pytest

from app.modules.checkup.web_research import research_web


class FakeSearch:
    def __init__(self, results=None, page=None, error=None, fetch_error=None):
        self.results = results or []
        self.page = page
        self.error = error
        self.fetch_error = fetch_error
        self.calls = []
        self.fetched = []

    def search(self, query, *, count=5, need_content=False, ori_question=None):
        self.calls.append({"query": query, "count": count, "need_content": need_content,
                           "ori_question": ori_question})
        if self.error:
            raise self.error
        return self.results

    def fetch(self, url):
        self.fetched.append(url)
        if self.fetch_error:
            raise self.fetch_error
        return self.page or {"title": "标题", "content": "正文"}


def _item(index, url=None, authority=3):
    return {
        "title": f"结果{index}",
        "url": url or f"https://example.gov.cn/{index}",
        "summary": "摘要",
        "date": "2026-09-01T00:00:00+00:00",
        "site_name": "example.gov.cn",
        "authority": authority,
        "rerank_score": 1.6,
    }


def test_research_reads_at_most_two_pages_and_counts_attempts():
    search = FakeSearch([_item(1), _item(2), _item(3)])
    result = research_web(search, "查询词", question="原始问题")

    assert result.status == "searched"
    assert result.attempts == 3  # one search + two page reads
    assert len(result.pages) == 2
    assert search.calls[0]["ori_question"] == "原始问题"


def test_research_caps_sources_and_filters_non_http_urls():
    search = FakeSearch([_item(i) for i in range(8)] + [_item(99, url="javascript:alert(1)")])
    result = research_web(search, "查询词")

    assert len(result.sources) == 5
    assert all(source.url.startswith("https://") for source in result.sources)


def test_research_reports_no_results_without_error():
    result = research_web(FakeSearch([]), "查询词")

    assert result.status == "no_results"
    assert result.error is None
    assert result.sources == []


def test_research_surfaces_a_search_failure():
    result = research_web(FakeSearch(error=RuntimeError("rate limited")), "查询词")

    assert result.status == "failed"
    assert "rate limited" in result.error
    assert result.sources == []


def test_research_keeps_summaries_when_a_page_read_fails():
    search = FakeSearch([_item(1)], fetch_error=RuntimeError("fetch 404"))
    result = research_web(search, "查询词")

    assert result.status == "searched"
    assert len(result.sources) == 1
    assert result.pages == []
    # The failed page read still counts as an attempt.
    assert result.attempts == 2


def test_research_without_fetch_capability_still_returns_summaries():
    class SearchOnly:
        def search(self, query, *, count=5, need_content=False, ori_question=None):
            return [_item(1)]

    result = research_web(SearchOnly(), "查询词")

    assert result.status == "searched"
    assert len(result.sources) == 1
    assert result.pages == []
