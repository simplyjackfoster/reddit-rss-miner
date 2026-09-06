import pytest
import requests

from reddit_rss_miner.config import Credentials
from reddit_rss_miner.transport import RateLimitedTransport, RateLimitExceeded


class Resp:
    def __init__(self, status, content=b"<x/>", headers=None):
        self.status_code, self.content, self.headers = status, content, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class Session:
    def __init__(self, responses):
        self.responses, self.calls, self.headers = list(responses), [], {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params)))
        return self.responses.pop(0)


def make(responses, **kw):
    slept = []
    s = Session(responses)
    t = RateLimitedTransport(Credentials("tok", "usr"), session=s, sleep=slept.append, **kw)
    return t, s, slept


def test_adds_auth_drops_none_and_sets_ua():
    t, s, _ = make([Resp(200, b"ok")])
    assert t.get("/r/x/search.rss", {"q": "a", "after": None}) == b"ok"
    url, params = s.calls[0]
    assert url == "https://www.reddit.com/r/x/search.rss"
    assert params == {"q": "a", "feed": "tok", "user": "usr"}
    assert "Mozilla" in s.headers["User-Agent"]


def test_429_sleeps_for_reset_plus_one_then_retries():
    t, s, slept = make([Resp(429, headers={"X-Ratelimit-Reset": "12"}), Resp(200, b"later")])
    assert t.get("/p") == b"later"
    assert slept == [13.0] and len(s.calls) == 2


def test_gives_up_after_max_attempts():
    t, _, slept = make([Resp(429)] * 3, max_attempts=3)
    with pytest.raises(RateLimitExceeded):
        t.get("/p")
    assert slept == [31.0, 31.0, 31.0]


def test_http_error_propagates():
    t, _, _ = make([Resp(503)])
    with pytest.raises(requests.HTTPError):
        t.get("/p")
