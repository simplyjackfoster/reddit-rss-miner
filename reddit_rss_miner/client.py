"""Reddit RSS client: knows Reddit's URL shapes, nothing about filtering or batches.

Verified search operators on ``search.rss``: quoted phrases, ``title:``, ``selftext:``, boolean OR,
and ``r/a+b`` multi-subreddit paths.
"""
from __future__ import annotations

import re
from typing import Iterator, Protocol

from .feed import Entry, parse_entries
from .transport import Transport

SEARCH_PAGE_MAX = 100
COMMENTS_LIMIT = 500
COMMENTS_DEPTH = 10

_POST_PATH = re.compile(r"/comments/([a-z0-9]+)(/[^/]*)?/?$")


class PostSearcher(Protocol):
    def search(self, subreddit: str, query: str, limit: int = 25, sort: str = "relevance",
               t: str = "all") -> Iterator[Entry]: ...


class CommentFetcher(Protocol):
    def comments(self, post_url_or_path: str) -> tuple[Entry | None, list[Entry]]: ...


def normalize_post_path(post_url_or_path: str) -> str:
    """'https://www.reddit.com/r/x/comments/abc/slug/' -> '/r/x/comments/abc/'."""
    path = re.sub(r"^https?://(?:www\.|old\.)?reddit\.com", "", post_url_or_path.strip())
    path = "/" + path.strip("/")
    return _POST_PATH.sub(r"/comments/\1/", path)


class ListingReader(Protocol):
    def listing_page(self, subreddit: str, sort: str = "new", after: str | None = None,
                     limit: int = SEARCH_PAGE_MAX) -> list[Entry]: ...


class RedditRSSClient:
    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def listing_page(self, subreddit: str, sort: str = "new", after: str | None = None,
                     limit: int = SEARCH_PAGE_MAX) -> list[Entry]:
        """One page of /r/<sub>/<sort>.rss. Reddit listings end at ~1,000 items regardless of history."""
        xml = self._transport.get(f"/r/{subreddit}/{sort}.rss", {"limit": min(SEARCH_PAGE_MAX, limit), "after": after})
        return list(parse_entries(xml))

    def search(self, subreddit: str, query: str, limit: int = 25, sort: str = "relevance",
               t: str = "all") -> Iterator[Entry]:
        """Yield posts matching ``query`` in ``subreddit`` (``a+b`` pools several), paginating by id."""
        after: str | None = None
        got = 0
        while got < limit:
            xml = self._transport.get(
                f"/r/{subreddit}/search.rss",
                {"q": query, "restrict_sr": 1, "sort": sort, "t": t,
                 "limit": min(SEARCH_PAGE_MAX, limit - got), "after": after},
            )
            page = list(parse_entries(xml))
            if not page:
                return
            for entry in page:
                yield entry
                got += 1
                if got >= limit:
                    return
            after = page[-1].id

    def comments(self, post_url_or_path: str) -> tuple[Entry | None, list[Entry]]:
        """Return (post, comments). Comments are flat; RSS carries no score, parent or depth."""
        path = normalize_post_path(post_url_or_path)
        xml = self._transport.get(f"{path}.rss", {"limit": COMMENTS_LIMIT, "depth": COMMENTS_DEPTH, "sort": "confidence"})
        entries = list(parse_entries(xml))
        post = next((e for e in entries if e.is_post), None)
        return post, [e for e in entries if e.is_comment]
