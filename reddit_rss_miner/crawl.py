"""Dedicated-subreddit crawl: every post and comment thread, no relevance filtering.

A product's own subreddit is on-topic by construction, so rows are emitted unfiltered with
``matched_in = "dedicated_subreddit"`` in the same schema batch mode uses, which keeps the two
outputs mergeable downstream.

Resume: the output JSONL is the checkpoint. Rows for one post are appended in a single write, so a
kill loses at most the post in flight. ``--resume`` reads the file, skips posts already present, and
continues listing pagination from the last post id in the file.

Ceiling: Reddit listings stop at ~1,000 items (fewer once removed posts drop out; measured 988 on a
large subreddit). When the listing exhausts at or above ``CEILING_SUSPECT_AT`` the crawl reports
``ceiling_suspected`` and says so on stderr, because that is a platform limit, not the end of the
subreddit's history. Search with ``--time`` filters reaches further back.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from .authors import AuthorFlagger
from .batch import Row, subreddit_of
from .client import CommentFetcher, ListingReader
from .feed import Entry
from .transport import redact

MATCHED_IN_DEDICATED = "dedicated_subreddit"
LISTING_CEILING = 1000
CEILING_SUSPECT_AT = 900


class CrawlClient(ListingReader, CommentFetcher, Protocol):
    """What the crawler needs: list a subreddit page and fetch a post's comments."""


class CrawlReporter(Protocol):
    def page(self, listed: int, new_on_page: int) -> None: ...
    def post_done(self, done: int, rows_total: int, elapsed: float) -> None: ...
    def fetch_error(self, post_id: str, error: Exception) -> None: ...
    def finished(self, summary: "CrawlSummary") -> None: ...


class NullCrawlReporter:
    def page(self, *a, **k) -> None: ...
    def post_done(self, *a, **k) -> None: ...
    def fetch_error(self, *a, **k) -> None: ...
    def finished(self, *a, **k) -> None: ...


class StderrCrawlReporter:
    def __init__(self, every: int = 10, stream=None) -> None:
        self._every, self._stream = every, stream or sys.stderr

    def _log(self, msg: str) -> None:
        print(msg, file=self._stream, flush=True)

    def page(self, listed, new_on_page):
        self._log(f"[listing] {listed} posts listed so far (+{new_on_page} this page)")

    def post_done(self, done, rows_total, elapsed):
        if self._every and done % self._every == 0:
            self._log(f"[crawl] {done} posts fetched | rows {rows_total} | elapsed {int(elapsed)//60}:{int(elapsed)%60:02d}")

    def fetch_error(self, post_id, error):
        self._log(f"[fetch error] {post_id} {type(error).__name__}: {redact(str(error))[:120]} -> skipped, continuing")

    def finished(self, s):
        if s.ceiling_suspected:
            self._log(f"[ceiling] Listing exhausted at {s.posts_listed_total} posts. This is Reddit's ~{LISTING_CEILING}-item "
                      "listing ceiling, a platform limit, NOT confirmation that no older history exists. "
                      "Use search mode with --time year/month to reach further back.")
        elif s.exhausted:
            self._log(f"[listing] exhausted at {s.posts_listed_total} posts: below the ceiling, so this is the whole listing")
        self._log(f"[crawl] done: {s.posts_fetched} posts fetched ({s.posts_skipped_resume} skipped as already present), "
                  f"{s.comments_total} comments, {s.rows_written} rows written, {s.fetch_errors} fetch errors")


@dataclass
class CrawlSummary:
    subreddit: str
    sort: str
    posts_listed_total: int = 0            # includes posts skipped via resume
    posts_fetched: int = 0
    posts_skipped_resume: int = 0
    comments_total: int = 0
    rows_written: int = 0
    fetch_errors: int = 0
    failed_posts: list[dict] = field(default_factory=list)
    exhausted: bool = False                # listing returned an empty page
    stopped_at_max: bool = False           # --max-posts reached
    ceiling_suspected: bool = False        # exhausted at >= CEILING_SUSPECT_AT
    last_post_id: str | None = None
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {**self.__dict__, "elapsed_seconds": round(self.elapsed_seconds, 1)}


class RowAppender(Protocol):
    def append(self, rows: list[Row]) -> None: ...


class JsonlAppender:
    """Appends each call's rows in ONE write so a kill never leaves half a post's rows behind."""

    def __init__(self, path: str | Path, resume: bool) -> None:
        self.path = Path(path)
        self._f = self.path.open("a" if resume else "w")

    def append(self, rows: list[Row]) -> None:
        if rows:
            self._f.write("".join(json.dumps(r.to_dict(), ensure_ascii=False) + "\n" for r in rows))
            self._f.flush()

    def close(self) -> None:
        self._f.close()


@dataclass(frozen=True)
class ResumeState:
    done_post_ids: frozenset[str]
    last_post_id: str | None


def read_resume_state(path: str | Path) -> ResumeState:
    """Posts already in the output, in the order they were written. Malformed trailing lines are ignored."""
    p = Path(path)
    if not p.exists():
        return ResumeState(frozenset(), None)
    seen: list[str] = []
    for line in p.read_text().splitlines():
        try:
            pid = json.loads(line)["post_id"]
        except (ValueError, KeyError):
            continue
        if not seen or seen[-1] != pid:
            seen.append(pid)
    return ResumeState(frozenset(seen), seen[-1] if seen else None)


class SubredditCrawler:
    def __init__(self, client: CrawlClient, appender: RowAppender, flagger: AuthorFlagger | None = None,
                 reporter: CrawlReporter | None = None, clock: Callable[[], float] | None = None) -> None:
        self._client = client
        self._out = appender
        self._flagger = flagger or AuthorFlagger()
        self._report = reporter or NullCrawlReporter()
        self._clock = clock or (lambda: time.time())

    def crawl(self, subreddit: str, sort: str = "new", max_posts: int = 0,
              resume: ResumeState | None = None) -> CrawlSummary:
        """``max_posts`` caps the total number of posts in the output file (0 = until the listing ends);
        with ``resume`` it therefore counts posts already present."""
        started = self._clock()
        resume = resume or ResumeState(frozenset(), None)
        s = CrawlSummary(subreddit=subreddit, sort=sort, last_post_id=resume.last_post_id,
                         posts_listed_total=len(resume.done_post_ids))
        after = resume.last_post_id
        done = set(resume.done_post_ids)
        while True:
            page = self._client.listing_page(subreddit, sort, after=after)
            if not page:
                s.exhausted = True
                break
            s.posts_listed_total += len(page)
            self._report.page(s.posts_listed_total, len(page))
            for post in page:
                if post.id in done:
                    s.posts_skipped_resume += 1
                    continue
                self._fetch_and_write(post, s)
                done.add(post.id)
                s.last_post_id = post.id
                self._report.post_done(s.posts_fetched, s.rows_written, self._clock() - started)
                if max_posts and len(done) >= max_posts:       # total posts in the output, resumed ones included
                    s.stopped_at_max = True
                    break
            if s.stopped_at_max:
                break
            after = page[-1].id
        s.ceiling_suspected = s.exhausted and s.posts_listed_total >= CEILING_SUSPECT_AT
        s.elapsed_seconds = self._clock() - started
        self._report.finished(s)
        return s

    def _fetch_and_write(self, post: Entry, s: CrawlSummary) -> None:
        try:
            full, comments = self._client.comments(post.url or "")
        except Exception as e:
            s.fetch_errors += 1
            s.failed_posts.append({"post_id": post.id, "url": post.url, "error": redact(f"{type(e).__name__}: {e}")[:200]})
            self._report.fetch_error(post.id, e)
            return
        post = full or post
        rows = [self._row(post, post)] + [self._row(post, c) for c in comments]
        self._out.append(rows)
        s.posts_fetched += 1
        s.comments_total += len(comments)
        s.rows_written += len(rows)

    def _row(self, post: Entry, item: Entry) -> Row:
        return Row(
            sub=subreddit_of(post.url), post_id=post.id, post_title=post.title,
            item_id=item.id, item_type="post" if item is post else "comment",
            author=item.author, body=item.body, url=item.url, updated=item.updated,
            links=list(item.links), images=list(item.images),
            matched_terms=[], matched_in=MATCHED_IN_DEDICATED, snippet=None,
            author_flags=self._flagger.flags(item.author),
        )
