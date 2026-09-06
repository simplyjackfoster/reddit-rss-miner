"""HTTP transport: one job, fetch bytes from reddit.com politely.

Adds the browser User-Agent Reddit expects, appends the feed credentials, drops ``None`` params,
sleeps on 429 using ``X-Ratelimit-Reset``, and gives up after a bounded number of retries.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Mapping, Protocol

import requests

from .config import Credentials

BASE_URL = "https://www.reddit.com"
DEFAULT_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:155.0) Gecko/20100101 Firefox/155.0"


class Transport(Protocol):
    def get(self, path: str, params: Mapping[str, Any] | None = None) -> bytes: ...


class RateLimitExceeded(RuntimeError):
    """Raised after repeated 429 responses."""


class RateLimitedTransport:
    def __init__(
        self,
        credentials: Credentials,
        session: requests.Session | None = None,
        base_url: str = BASE_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 30.0,
        max_attempts: int = 5,
        sleep: Callable[[float], None] = time.sleep,
        on_rate_limit: Callable[[float], None] | None = None,
    ) -> None:
        self._auth = credentials.as_params()
        self._session = session or requests.Session()
        self._session.headers["User-Agent"] = user_agent
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._on_rate_limit = on_rate_limit

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> bytes:
        query = {k: v for k, v in (params or {}).items() if v is not None}
        query.update(self._auth)
        for _ in range(self._max_attempts):
            response = self._session.get(f"{self._base_url}{path}", params=query, timeout=self._timeout)
            if response.status_code == 429:
                wait = float(response.headers.get("X-Ratelimit-Reset", 30)) + 1
                if self._on_rate_limit:
                    self._on_rate_limit(wait)
                self._sleep(wait)
                continue
            response.raise_for_status()
            return response.content
        raise RateLimitExceeded(f"gave up after {self._max_attempts} rate-limited attempts on {path}")
