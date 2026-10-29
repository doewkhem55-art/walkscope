"""DeepSeek official Search and Fetch API clients (web research for report questions).

Search returns structured results plus optional page content; Fetch retrieves one
page as Markdown. Both are separate HTTP endpoints from the chat model: the
application decides when to call them and what to give the model. They count
towards the per-question HTTP attempt budget enforced by the caller.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

SEARCH_URL = "https://api.deepseek.com/v1/tools/search"
FETCH_URL = "https://api.deepseek.com/v1/tools/fetch"
DEFAULT_TIMEOUT_SECONDS = 15.0


class SearchError(RuntimeError):
    """A web research call failed with a message that is safe to show to a user."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class DeepSeekSearch:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = SEARCH_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("DeepSeek Search 需要非空 API key")
        self._api_key = api_key.strip()
        self._url = base_url
        self._fetch_url = FETCH_URL if base_url == SEARCH_URL else base_url.replace("/search", "/fetch")
        self._timeout = timeout_seconds
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._owns_client = client is None
        self._sleep = sleep

    def search(
        self,
        query: str,
        *,
        count: int = 5,
        need_content: bool = False,
        ori_question: str | None = None,
    ) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        count = max(1, min(50, int(count)))
        payload: dict[str, Any] = {"query": query[:128], "count": count, "need_content": need_content}
        if ori_question and ori_question.strip():
            payload["ori_question"] = ori_question.strip()[:128]
        data = self._post(self._url, payload, "联网搜索")
        results = data.get("results") if isinstance(data, Mapping) else None
        if not isinstance(results, list):
            raise SearchError("联网搜索响应格式无效。", retryable=False)
        return [item for item in results if isinstance(item, Mapping)]

    def fetch(self, url: str) -> dict[str, Any]:
        if not isinstance(url, str) or not url.strip():
            raise SearchError("网页抓取需要有效地址。", retryable=False)
        data = self._post(self._fetch_url, {"url": url.strip()[:8192]}, "网页抓取")
        content = data.get("content") if isinstance(data, Mapping) else None
        if not isinstance(content, str):
            raise SearchError("网页抓取没有返回正文。", retryable=False)
        title = data.get("title") if isinstance(data, Mapping) else None
        return {"title": title if isinstance(title, str) else "", "content": content}

    def _post(self, url: str, payload: dict[str, Any], label: str) -> dict[str, Any]:
        last_error: SearchError | None = None
        for attempt in range(2):
            try:
                response = self._client.post(
                    url,
                    headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                    json=payload,
                )
            except httpx.HTTPError:
                last_error = SearchError(f"{label}请求失败，请稍后重试。", retryable=True)
                if attempt == 0:
                    self._sleep(0.5)
                    continue
                raise last_error from None
            if response.status_code == 200:
                data = response.json()
                if not isinstance(data, Mapping):
                    raise SearchError(f"{label}响应格式无效。", retryable=False)
                return dict(data)
            last_error = self._error_for(response.status_code, label)
            if last_error.retryable and attempt == 0:
                self._sleep(1.0)
                continue
            raise last_error
        raise last_error or SearchError(f"{label}失败。", retryable=True)

    @staticmethod
    def _error_for(status_code: int, label: str) -> SearchError:
        if status_code in {408, 429} or status_code >= 500:
            return SearchError(f"{label}服务繁忙或限流，请稍后重试。", retryable=True)
        if status_code == 402:
            return SearchError(f"{label}账户余额不足。", retryable=False)
        if status_code in {401, 403}:
            return SearchError(f"{label}鉴权或安全策略拒绝了本次请求。", retryable=False)
        if status_code == 404:
            return SearchError(f"{label}没有找到可用内容。", retryable=False)
        return SearchError(f"{label}返回 HTTP {status_code}。", retryable=False)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
