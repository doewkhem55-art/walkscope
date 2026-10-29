"""One-run Baidu transport with success caching and finite per-endpoint rotation.

Pass the instance as ``transport=`` (or POI's ``http_get=``). Credentials stay
in memory; the atomic JSON ledger contains aliases and endpoint paths only.
The existing single-worker execution model owns serialization of calls.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import urlopen

from app.errors import ProviderStopError


_ROTATE_STATUSES = {3, 4, 5, 101, 102, 200, 201, 202, 203, 210, 211, 220, 230, 240, 301, 302}


def _get_json(url: str, timeout: float) -> Mapping[str, Any]:
    with urlopen(url, timeout=timeout) as response:
        return json.load(response)


def _atomic_json(path: Path, value: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    except OSError:
        raise OSError("Could not persist Baidu request accounting or cache.") from None


class BoundedBaiduTransport:
    def __init__(
        self,
        credentials: Sequence[Mapping[str, str]],
        cache_dirs: Sequence[Path],
        ledger_path: Path,
        *,
        max_requests: int = 450,
        max_seconds: float = 1200,
        min_interval: float = 1.5,
        route_pair_qps: float | None = None,
        get_json: Callable[[str, float], Mapping[str, Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not credentials or not cache_dirs:
            raise ValueError("Credentials and at least one cache directory are required.")
        pairs = [(item.get("alias"), item.get("value")) for item in credentials]
        if any(not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", alias)
               or not isinstance(value, str) or not value.strip() for alias, value in pairs):
            raise ValueError("Credentials require safe aliases and nonempty values.")
        if len({alias for alias, _ in pairs}) != len(pairs) or len({value for _, value in pairs}) != len(pairs):
            raise ValueError("Credential aliases and values must be distinct.")
        if any(value in alias for alias, _ in pairs for _, value in pairs):
            raise ValueError("Credential aliases must not contain credential values.")
        if (isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 0
                or not math.isfinite(max_seconds) or max_seconds < 0
                or not math.isfinite(min_interval) or min_interval < 0):
            raise ValueError("Request and time limits must be finite and nonnegative.")
        self._credentials = pairs
        self._cache_dirs = [Path(folder) for folder in cache_dirs]
        self._ledger_path = Path(ledger_path)
        self._max_requests = max_requests
        self._max_seconds = max_seconds
        self._min_interval = min_interval
        if route_pair_qps is not None and (not math.isfinite(route_pair_qps) or route_pair_qps <= 0):
            raise ValueError('Route-pair concurrency must be finite and positive.')
        self._route_pair_qps = route_pair_qps
        self._last_cost = 1
        self._get_json = get_json or _get_json
        self._clock, self._sleep = clock, sleep
        self._started = clock()
        self._last_call: float | None = None
        self.requests = self.cache_hits = 0
        self._requests_by_alias = {alias: 0 for alias, _ in pairs}
        self._status_counts: Counter[str] = Counter()
        self._attempts: list[dict[str, Any]] = []
        self._key_index: dict[str, int] = {}
        self._exhausted: dict[str, Mapping[str, Any]] = {}

    def snapshot(self) -> dict[str, Any]:
        return dict(
            requests=self.requests, cache_hits=self.cache_hits,
            elapsed=round(max(0.0, self._clock() - self._started), 3),
            max_requests=self._max_requests, max_seconds=self._max_seconds,
            requests_by_alias=dict(self._requests_by_alias),
            status_counts=dict(self._status_counts),
            endpoint_exhausted=sorted(self._exhausted),
            **({'route_pair_qps': self._route_pair_qps} if self._route_pair_qps is not None else {}),
        )

    def _persist(self) -> None:
        _atomic_json(self._ledger_path, dict(self.snapshot(), attempts=self._attempts))

    def _redact_error(self, value: Any) -> Any:
        if isinstance(value, str):
            for _, secret in self._credentials:
                value = value.replace(secret, "[redacted]")
            return re.sub(r"https?://[^\s<>\"']+", "[redacted-url]", value)
        if isinstance(value, Mapping):
            return {self._redact_error(key): self._redact_error(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact_error(item) for item in value]
        return value

    def _remaining(self) -> float:
        remaining = self._max_seconds - (self._clock() - self._started)
        if self.requests >= self._max_requests or remaining <= 0:
            raise ProviderStopError("PROVIDER_BUDGET_EXHAUSTED", "Baidu request count or active time limit reached.")
        return remaining

    def __call__(self, url: str, timeout: float) -> Mapping[str, Any]:
        parts = urlsplit(url)
        endpoint = parts.path
        if (parts.scheme != "https" or parts.hostname != "api.map.baidu.com"
                or parts.username or parts.password
                or any(value in endpoint for _, value in self._credentials)):
            raise ValueError("A valid Baidu HTTPS endpoint is required.")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Request timeout must be finite and positive.")
        query = [(name, value) for name, value in parse_qsl(parts.query) if name != "ak"]
        public = endpoint + "?" + urlencode(query)
        filename = hashlib.sha256(public.encode()).hexdigest() + ".json"
        for folder in self._cache_dirs:
            try:
                cached = json.loads((folder / filename).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                continue
            if isinstance(cached, Mapping) and cached.get("status") == 0:
                self.cache_hits += 1
                return cached
        if endpoint in self._exhausted:
            return self._exhausted[endpoint]
        cost = 1
        if self._route_pair_qps is not None and endpoint.startswith('/routematrix/'):
            params = dict(query)
            cost = len(params.get('origins','').split('|')) * len(params.get('destinations','').split('|'))
            if cost > self._route_pair_qps:
                raise ValueError('Matrix batch exceeds configured route-pair concurrency; split targets before HTTP.')

        while True:
            remaining = self._remaining()
            if self._last_call is not None:
                interval = max(self._min_interval, self._last_cost/self._route_pair_qps + .05
                    if self._route_pair_qps is not None else self._min_interval)
                delay = max(0.0, interval - (self._clock() - self._last_call))
                if delay:
                    self._sleep(min(delay, remaining))
                remaining = self._remaining()
            index = self._key_index.get(endpoint, 0)
            alias, value = self._credentials[index]
            request_url = urlunsplit((parts.scheme, parts.netloc, endpoint, urlencode(query + [("ak", value)]), ""))
            self._last_call = self._clock()
            self._last_cost = cost
            self.requests += 1
            self._requests_by_alias[alias] += 1
            attempt = dict(attempt=self.requests, alias=alias, endpoint=endpoint, status="pending")
            self._attempts.append(attempt)
            # Count every HTTP attempt durably before handing control to urllib.
            self._persist()
            safe_error: Exception | None = None
            try:
                payload = self._get_json(request_url, min(timeout, remaining))
            except HTTPError as error:
                attempt["status"] = "http_" + str(error.code)
                safe_error = HTTPError(endpoint, error.code, "Baidu HTTP request failed.", None, None)
            except (TimeoutError,):
                attempt["status"] = "timeout"
                safe_error = TimeoutError("Baidu request timed out.")
            except URLError:
                attempt["status"] = "network_error"
                safe_error = URLError("Baidu request failed.")
            except Exception:
                attempt["status"] = "network_error"
                safe_error = OSError("Baidu request failed or returned invalid JSON.")
            if safe_error is not None:
                self._status_counts[str(attempt["status"])] += 1
                self._persist()
                # Raise outside the except block so even exception context has no URL/AK.
                raise safe_error from None
            if not isinstance(payload, Mapping):
                attempt["status"] = "invalid_response"
                self._status_counts["invalid_response"] += 1
                self._persist()
                raise ValueError("Baidu response must be a JSON object.")
            raw_status = payload.get("status")
            status = raw_status if isinstance(raw_status, int) and not isinstance(raw_status, bool) else None
            if isinstance(raw_status, str) and raw_status.isdecimal():
                status = int(raw_status)
            attempt["status"] = status if status is not None else "invalid_response"
            self._status_counts[str(attempt["status"])] += 1
            if raw_status != 0 or isinstance(raw_status, bool):
                payload = self._redact_error(payload)
            if status in _ROTATE_STATUSES:
                self._key_index[endpoint] = index + 1
                if index + 1 == len(self._credentials):
                    self._exhausted[endpoint] = payload
            self._persist()
            if raw_status == 0 and not isinstance(raw_status, bool):
                _atomic_json(self._cache_dirs[0] / filename, payload)
            if status not in _ROTATE_STATUSES or endpoint in self._exhausted:
                return payload
