"""Batch mining: subreddits x product terms, per-item relevance filtering, disclosed stats.

Why the filter exists: Reddit search matches comment text, so a 500-comment megathread becomes a
hit when one comment names a product, and every comment in it looks like data. Common-word
product names collide with ordinary English. Rule: a post is a row only if its own title/body
names the product; a comment only if the comment itself does. Every discard is counted by reason.

Safeguards: zero-hit searches are retried once (Reddit occasionally returns a transient empty
feed); a per-term circuit breaker stops fetching comments for terms that produce no rows after
``breaker_after`` posts; a failed fetch is recorded and skipped rather than aborting the run;
each term gets a verdict (ok | confirmed_zero_presence | hits_but_no_matches | breaker_tripped).
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol, Sequence

from .client import CommentFetcher, PostSearcher
from .feed import Entry
from .terms import DISCARD_REASONS, Term
from .transport import redact

VERDICT_OK = "ok"
VERDICT_ZERO = "confirmed_zero_presence"
VERDICT_NO_MATCHES = "hits_but_no_matches"
VERDICT_BREAKER = "breaker_tripped"

MATCHED_IN_POST = "post"
MATCHED_IN_COMMENT = "comment"
MATCHED_IN_THREAD_CONTEXT = "thread_context"


class MinerClient(PostSearcher, CommentFetcher, Protocol):
    """What the runner needs from a client: search posts and fetch a post's comments."""


# --------------------------------------------------------------------------- options


@dataclass(frozen=True)
class BatchOptions:
    limit: int = 10
    sort: str = "relevance"
    time_filter: str = "all"
    delay: float = 1.0                 # accepted for compatibility; pacing is enforced by the transport's Pacer
    thread_context: bool = False
    scope: str = "all"                 # all | title | selftext
    breaker_after: int = 12            # 0 disables
    progress_every: int = 10           # 0 disables
    zero_retry_delay: float = 8.0
    per_sub: bool = False

    def search_query(self, term: Term) -> str:
        return f"{self.scope}:{term.search}" if self.scope in ("title", "selftext") else term.search


# --------------------------------------------------------------------------- models


@dataclass
class Row:
    sub: str | None
    post_id: str
    post_title: str
    item_id: str
    item_type: str
    author: str | None
    body: str
    url: str | None
    updated: str
    links: list[str]
    images: list[str]
    matched_terms: list[str]
    matched_in: str
    snippet: str | None

    def to_dict(self) -> dict:
        return {
            "sub": self.sub, "post_id": self.post_id, "post_title": self.post_title,
            "item_id": self.item_id, "item_type": self.item_type, "author": self.author,
            "body": self.body, "url": self.url, "updated": self.updated,
            "links": self.links, "images": self.images,
            "matched_terms": self.matched_terms, "matched_in": self.matched_in, "snippet": self.snippet,
        }


@dataclass
class TermStats:
    search_hits: int = 0
    search_attempts: int = 0
    zero_hits_recovered_on_retry: bool = False
    post_level_hits: int = 0
    post_level_precision: float | None = None
    posts_evaluated: int = 0
    posts_kept: int = 0
    comments_fetched: int = 0
    comments_kept: int = 0
    breaker_tripped: bool = False
    breaker_tripped_after_posts: int | None = None
    verdict: str | None = None

    @property
    def produced_rows(self) -> bool:
        return self.posts_kept > 0 or self.comments_kept > 0

    def decide_verdict(self) -> str:
        if self.search_hits == 0:
            self.verdict = VERDICT_ZERO
        elif self.breaker_tripped:
            self.verdict = VERDICT_BREAKER
        elif not self.produced_rows:
            self.verdict = VERDICT_NO_MATCHES
        else:
            self.verdict = VERDICT_OK
        return self.verdict

    def to_dict(self) -> dict:
        d = {
            "search_hits": self.search_hits, "search_attempts": self.search_attempts,
            "zero_hits_recovered_on_retry": self.zero_hits_recovered_on_retry,
            "post_level_hits": self.post_level_hits, "post_level_precision": self.post_level_precision,
            "posts_evaluated": self.posts_evaluated, "posts_kept": self.posts_kept,
            "comments_fetched": self.comments_fetched, "comments_kept": self.comments_kept,
            "breaker_tripped": self.breaker_tripped, "verdict": self.verdict,
        }
        if self.breaker_tripped:
            d["breaker_tripped_after_posts"] = self.breaker_tripped_after_posts
        return d


@dataclass
class BatchStats:
    per_search: dict[str, TermStats] = field(default_factory=dict)
    discarded: dict[str, int] = field(default_factory=lambda: {r: 0 for r in DISCARD_REASONS})
    posts_seen: int = 0
    posts_deduped: int = 0
    comment_fetches: int = 0
    comments_total: int = 0
    posts_skipped_by_breaker: int = 0
    fetch_errors: int = 0
    failed_posts: list[dict] = field(default_factory=list)
    rows: dict[str, int] = field(default_factory=lambda: {"post": 0, "comment": 0, "thread_context": 0})
    elapsed_seconds: float = 0.0

    @property
    def unique_posts(self) -> int:
        return self.posts_seen - self.posts_deduped

    def summary(self) -> dict:
        kept = self.rows["post"] + self.rows["comment"]
        candidates = self.unique_posts + self.comments_total
        return {
            "unique_posts": self.unique_posts, "comments_total": self.comments_total,
            "candidates_evaluated": candidates, "rows_kept_strict": kept,
            "rows_thread_context": self.rows["thread_context"],
            "discard_rate_strict": round(1 - kept / candidates, 4) if candidates else None,
            "posts_skipped_by_breaker": self.posts_skipped_by_breaker,
            "fetch_errors": self.fetch_errors,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "verdicts": {k: v.verdict for k, v in self.per_search.items()},
        }

    def to_dict(self) -> dict:
        return {
            "per_search": {k: v.to_dict() for k, v in self.per_search.items()},
            "discarded": dict(self.discarded),
            "posts_seen": self.posts_seen, "posts_deduped": self.posts_deduped,
            "comment_fetches": self.comment_fetches, "comments_total": self.comments_total,
            "posts_skipped_by_breaker": self.posts_skipped_by_breaker,
            "fetch_errors": self.fetch_errors, "failed_posts": list(self.failed_posts),
            "rows": dict(self.rows),
            "summary": self.summary(),
        }


@dataclass
class BatchResult:
    rows: list[Row]
    stats: BatchStats


# --------------------------------------------------------------------------- reporting


class Reporter(Protocol):
    def search_retry(self, index: int, total: int, key: str, delay: float) -> None: ...
    def search_done(self, index: int, total: int, key: str, stats: TermStats) -> None: ...
    def breaker_tripped(self, key: str, stats: TermStats) -> None: ...
    def fetch_error(self, post_id: str, error: Exception) -> None: ...
    def progress(self, done: int, total: int, rows: int, fetches: int, elapsed: float, eta: float) -> None: ...
    def finished_with_skips(self, total: int, rows: int, fetches: int, skipped: int) -> None: ...


class NullReporter:
    def search_retry(self, *a, **k) -> None: ...
    def search_done(self, *a, **k) -> None: ...
    def breaker_tripped(self, *a, **k) -> None: ...
    def fetch_error(self, *a, **k) -> None: ...
    def progress(self, *a, **k) -> None: ...
    def finished_with_skips(self, *a, **k) -> None: ...


def _mmss(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


class StderrReporter:
    def __init__(self, stream=None) -> None:
        self._stream = stream or sys.stderr

    def _log(self, msg: str) -> None:
        print(msg, file=self._stream, flush=True)

    def search_retry(self, index, total, key, delay):
        self._log(f"[search {index}/{total}] {key}: 0 hits, retrying once in {delay:.0f}s")

    def search_done(self, index, total, key, stats):
        note = " (recovered after empty first response)" if stats.zero_hits_recovered_on_retry else ""
        self._log(f"[search {index}/{total}] {key}: {stats.search_hits} hits, "
                  f"{stats.post_level_hits} name the product in title/body{note}")

    def breaker_tripped(self, key, stats):
        self._log(f"[breaker] {key}: 0 rows after {stats.posts_evaluated} posts / {stats.comments_fetched} comments. "
                  "Skipping its remaining posts; query looks too generic.")

    def fetch_error(self, post_id, error):
        self._log(f"[fetch error] {post_id} {type(error).__name__}: {redact(str(error))[:120]} -> skipped, continuing")

    def progress(self, done, total, rows, fetches, elapsed, eta):
        self._log(f"[{done}/{total} posts] rows {rows} | fetches {fetches} | elapsed {_mmss(elapsed)} | eta ~{_mmss(eta)}")

    def finished_with_skips(self, total, rows, fetches, skipped):
        self._log(f"[{total}/{total} posts] rows {rows} | fetches {fetches} | skipped by breaker {skipped} | done")


# --------------------------------------------------------------------------- breaker


class CircuitBreaker:
    """Trips a (group, term) key after ``after`` evaluated posts with zero rows. ``after=0`` disables."""

    def __init__(self, after: int) -> None:
        self._after = after
        self._tripped: set[str] = set()

    def is_tripped(self, key: str) -> bool:
        return key in self._tripped

    def record_evaluated(self, key: str, stats: TermStats) -> bool:
        """Count one evaluated post; return True if this call tripped the key."""
        stats.posts_evaluated += 1
        if self._after and stats.posts_evaluated >= self._after and not stats.produced_rows and key not in self._tripped:
            self._tripped.add(key)
            stats.breaker_tripped = True
            stats.breaker_tripped_after_posts = stats.posts_evaluated
            return True
        return False


# --------------------------------------------------------------------------- runner


def subreddit_of(url: str | None) -> str | None:
    return url.split("/r/")[1].split("/")[0] if url and "/r/" in url else None


def _post_text(post: Entry) -> str:
    return f"{post.title}\n{post.body}"


@dataclass
class _Candidate:
    post: Entry
    hits: set[tuple[str, str]] = field(default_factory=set)   # (group, term name)


class BatchRunner:
    def __init__(
        self,
        client: MinerClient,
        terms: Mapping[str, Term],
        options: BatchOptions = BatchOptions(),
        reporter: Reporter | None = None,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._client = client
        self._terms = dict(terms)
        self._opt = options
        self._report = reporter or NullReporter()
        self._sleep = sleep or (lambda seconds: time.sleep(seconds))
        self._clock = clock or (lambda: time.time())

    @staticmethod
    def key(group: str, name: str) -> str:
        return f"{group} :: {name}"

    def run(self, subs: Sequence[str]) -> BatchResult:
        started = self._clock()
        stats = BatchStats()
        candidates, order = self._search_phase(subs, stats)
        rows = self._comment_phase(candidates, order, stats, started)
        for ts in stats.per_search.values():
            ts.decide_verdict()
        stats.elapsed_seconds = self._clock() - started
        return BatchResult(rows=rows, stats=stats)

    # ---- phase 1: searches with zero-hit retry ----
    def _search_phase(self, subs: Sequence[str], stats: BatchStats) -> tuple[dict[str, _Candidate], list[str]]:
        groups = list(subs) if self._opt.per_sub else ["+".join(subs)]
        candidates: dict[str, _Candidate] = {}
        order: list[str] = []
        jobs = [(g, term) for g in groups for term in self._terms.values()]
        for i, (group, term) in enumerate(jobs, 1):
            key = self.key(group, term.name)
            hits, attempts = self._search_with_retry(i, len(jobs), key, group, term)
            ts = TermStats(search_hits=len(hits), search_attempts=attempts,
                           zero_hits_recovered_on_retry=bool(hits) and attempts > 1)
            for post in hits:
                stats.posts_seen += 1
                if post.id in candidates:
                    stats.posts_deduped += 1
                else:
                    order.append(post.id)
                candidates.setdefault(post.id, _Candidate(post)).hits.add((group, term.name))
                if term.match(_post_text(post)).matched:
                    ts.post_level_hits += 1
            if hits:
                ts.post_level_precision = round(ts.post_level_hits / len(hits), 3)
            stats.per_search[key] = ts
            self._report.search_done(i, len(jobs), key, ts)
        return candidates, order

    def _search_with_retry(self, index: int, total: int, key: str, group: str, term: Term) -> tuple[list[Entry], int]:
        attempts = 0
        while True:
            attempts += 1
            hits = list(self._client.search(group, self._opt.search_query(term), limit=self._opt.limit,
                                            sort=self._opt.sort, t=self._opt.time_filter))
            if hits or attempts >= 2:
                return hits, attempts
            self._report.search_retry(index, total, key, self._opt.zero_retry_delay)
            self._sleep(self._opt.zero_retry_delay)

    # ---- phase 2: comments with circuit breaker ----
    def _comment_phase(self, candidates: dict[str, _Candidate], order: list[str],
                       stats: BatchStats, started: float) -> list[Row]:
        rows: list[Row] = []
        breaker = CircuitBreaker(self._opt.breaker_after)
        total = len(order)
        for n, pid in enumerate(order, 1):
            cand = candidates[pid]
            active = [(g, name) for (g, name) in cand.hits if not breaker.is_tripped(self.key(g, name))]
            if not active:
                stats.posts_skipped_by_breaker += 1
                if self._opt.progress_every and n == total:
                    self._report.finished_with_skips(total, len(rows), stats.comment_fetches, stats.posts_skipped_by_breaker)
                continue

            post_terms = self._evaluate_post(cand.post, active, stats)
            if post_terms:
                stats.rows["post"] += 1
                rows.append(self._row(cand.post, cand.post, MATCHED_IN_POST, [n for n, _ in post_terms], post_terms[0][1]))

            comments = self._fetch_comments(cand.post, stats)
            if comments is None:
                continue
            for (g, name) in active:
                stats.per_search[self.key(g, name)].comments_fetched += len(comments)
            rows.extend(self._evaluate_comments(cand.post, comments, active, post_terms, stats))

            for (g, name) in active:
                key = self.key(g, name)
                if breaker.record_evaluated(key, stats.per_search[key]):
                    self._report.breaker_tripped(key, stats.per_search[key])

            if self._opt.progress_every and (n % self._opt.progress_every == 0 or n == total):
                elapsed = self._clock() - started
                eta = (elapsed / n) * (total - n) if n else 0.0
                self._report.progress(n, total, len(rows), stats.comment_fetches, elapsed, eta)
        return rows

    def _evaluate_post(self, post: Entry, active, stats: BatchStats) -> list[tuple[str, str | None]]:
        matched: list[tuple[str, str | None]] = []
        text = _post_text(post)
        for (g, name) in active:
            result = self._terms[name].match(text)
            if result.matched:
                if name not in [x for x, _ in matched]:
                    matched.append((name, result.snippet))
                stats.per_search[self.key(g, name)].posts_kept += 1
            else:
                stats.discarded[result.reason] += 1
        return matched

    def _fetch_comments(self, post: Entry, stats: BatchStats) -> list[Entry] | None:
        try:
            _, comments = self._client.comments(post.url or "")
        except Exception as e:  # 5xx, timeout, malformed feed: record and keep going
            stats.fetch_errors += 1
            stats.failed_posts.append({"post_id": post.id, "url": post.url, "error": redact(f"{type(e).__name__}: {e}")[:200]})
            self._report.fetch_error(post.id, e)
            return None
        stats.comment_fetches += 1
        stats.comments_total += len(comments)
        return comments

    def _evaluate_comments(self, post: Entry, comments: list[Entry], active, post_terms, stats: BatchStats) -> list[Row]:
        rows: list[Row] = []
        for comment in comments:
            matched: list[tuple[str, str | None]] = []
            for (g, name) in active:
                result = self._terms[name].match(comment.body)
                if result.matched:
                    if name not in [x for x, _ in matched]:
                        matched.append((name, result.snippet))
                    stats.per_search[self.key(g, name)].comments_kept += 1
                else:
                    stats.discarded[result.reason] += 1
            if matched:
                stats.rows["comment"] += 1
                rows.append(self._row(post, comment, MATCHED_IN_COMMENT, [n for n, _ in matched], matched[0][1]))
            elif self._opt.thread_context and post_terms:
                stats.rows["thread_context"] += 1
                rows.append(self._row(post, comment, MATCHED_IN_THREAD_CONTEXT, [n for n, _ in post_terms], None))
        return rows

    @staticmethod
    def _row(post: Entry, item: Entry, matched_in: str, terms: list[str], snippet: str | None) -> Row:
        return Row(
            sub=subreddit_of(post.url), post_id=post.id, post_title=post.title,
            item_id=item.id, item_type=MATCHED_IN_POST if item is post else MATCHED_IN_COMMENT,
            author=item.author, body=item.body, url=item.url, updated=item.updated,
            links=list(item.links), images=list(item.images),
            matched_terms=terms, matched_in=matched_in, snippet=snippet,
        )


def run_batch(rd, subs, terms, limit, sort, t, delay, thread_context=False, scope="all",
              breaker_after=12, progress_every=10, zero_retry_delay=8.0, per_sub=False,
              reporter: Reporter | None = None) -> tuple[list[dict], dict]:
    """Facade with the single-file signature: returns (rows as dicts, stats as dict)."""
    options = BatchOptions(limit=limit, sort=sort, time_filter=t, delay=delay, thread_context=thread_context,
                           scope=scope, breaker_after=breaker_after, progress_every=progress_every,
                           zero_retry_delay=zero_retry_delay, per_sub=per_sub)
    result = BatchRunner(rd, terms, options, reporter or StderrReporter()).run(list(subs))
    return [r.to_dict() for r in result.rows], result.stats.to_dict()
