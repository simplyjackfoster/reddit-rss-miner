"""HTTP transport: one job, fetch bytes from reddit.com politely.

Adds the browser User-Agent Reddit expects, appends the feed credentials, drops ``None`` params,
asks the ``Pacer`` for a slot before every request, sleeps on 429 using ``X-Ratelimit-Reset`` (and
tells the pacer so sibling processes back off too), and gives up after a bounded number of retries.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Mapping, Protocol

import requests

from .config import Credentials
from .pacing import NoPacer, Pacer

BASE_URL = "https://www.reddit.com"
DEFAULT_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:155.0) Gecko/20100101 Firefox/155.0"


class Transport(Protocol):
    def get(self, path: str, params: Mapping[str, Any] | None = None) -> bytes: ...


class RateLimitExceeded(RuntimeError):
    """Raised after repeated 429 responses."""


class HttpError(RuntimeError):
    """Non-2xx response. The message never contains credentials: the URL is redacted first."""

    def __init__(self, status: int, path: str) -> None:
        super().__init__(f"HTTP {status} for {path}")
        self.status = status
        self.path = path


_CREDENTIAL_PARAMS = re.compile(r"([?&](?:feed|user)=)[^&\s]*", re.I)


def redact(text: str) -> str:
    """Strip feed token and username values from any URL-bearing text before it is logged or stored."""
    return _CREDENTIAL_PARAMS.sub(r"\1<redacted>", text)


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
        pacer: Pacer | None = None,
    ) -> None:
        self._auth = credentials.as_params()
        self._session = session or requests.Session()
        self._session.headers["User-Agent"] = user_agent
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._on_rate_limit = on_rate_limit
        self._pacer = pacer or NoPacer()

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> bytes:
        query = {k: v for k, v in (params or {}).items() if v is not None}
        query.update(self._auth)
        for _ in range(self._max_attempts):
            self._pacer.acquire()
            response = self._session.get(f"{self._base_url}{path}", params=query, timeout=self._timeout)
            if response.status_code == 429:
                wait = float(response.headers.get("X-Ratelimit-Reset", 30)) + 1
                self._pacer.penalize(wait)
                if self._on_rate_limit:
                    self._on_rate_limit(wait)
                self._sleep(wait)
                continue
            if response.status_code >= 400:
                raise HttpError(response.status_code, path)   # never requests' message: it embeds the full URL
            return response.content
        raise RateLimitExceeded(f"gave up after {self._max_attempts} rate-limited attempts on {path}")
