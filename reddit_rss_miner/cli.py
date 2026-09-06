"""Command line: argument parsing and the composition root where concrete classes are wired."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Sequence

from .authors import AuthorFlagger
from .batch import BatchOptions, BatchRunner, StderrReporter
from .client import RedditRSSClient
from .crawl import JsonlAppender, StderrCrawlReporter, SubredditCrawler, read_resume_state
from .config import MissingCredentials, load_credentials
from .pacing import FileLockPacer, state_path_for
from .terms import load_terms
from .transport import RateLimitedTransport
from .writers import JsonlFileSink, write_simple_results

PROG = "reddit-rss-miner"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=PROG,
        description="Search subreddits for product mentions and pull matching posts and comments via authenticated RSS.",
    )
    ap.add_argument("subreddit", nargs="?")
    ap.add_argument("query", nargs="?")
    ap.add_argument("--limit", type=int, default=10, help="posts per search (paginates past 100)")
    ap.add_argument("--sort", default="relevance", choices=["relevance", "hot", "top", "new", "comments"])
    ap.add_argument("--time", default="all", choices=["hour", "day", "week", "month", "year", "all"])
    ap.add_argument("--post", help="skip search; fetch this post (URL or r/sub/comments/id)")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="minimum seconds between requests, enforced across every process using the same token")
    ap.add_argument("--out", help="simple mode: write [{post, comments}] JSON here")
    ap.add_argument("--subs", help="BATCH: comma-separated subreddits, e.g. productivity,macapps")
    ap.add_argument("--terms", help="BATCH: terms.json (see terms.example.json)")
    ap.add_argument("--rows", default="rows.jsonl", help="BATCH: JSONL output (stats -> <rows>.stats.json)")
    ap.add_argument("--scope", default="all", choices=["all", "title", "selftext"],
                    help="BATCH: server-side pre-scope via Reddit title:/selftext: operators")
    ap.add_argument("--thread-context", action="store_true",
                    help="BATCH: also keep non-matching comments under matched posts (tagged thread_context)")
    ap.add_argument("--breaker", type=int, default=12,
                    help="BATCH: trip a term after this many fully-evaluated posts with zero rows; 0 disables")
    ap.add_argument("--progress", type=int, default=10, help="BATCH: progress line to stderr every N posts; 0 disables")
    ap.add_argument("--per-sub", action="store_true",
                    help="BATCH: one search per (subreddit, term) so --limit applies per subreddit, instead of one pooled r/a+b search")
    ap.add_argument("--full-subreddit", metavar="NAME",
                    help="CRAWL: every post + comment thread of one subreddit, unfiltered, rows tagged dedicated_subreddit; writes --rows")
    ap.add_argument("--listing-sort", default="new", choices=["new", "hot", "top", "rising"], help="CRAWL: listing order")
    ap.add_argument("--max-posts", type=int, default=0, help="CRAWL: cap on total posts in the --rows file (resumed ones count); 0 = until the listing ends")
    ap.add_argument("--resume", action="store_true", help="CRAWL: continue an interrupted crawl using the existing --rows file as the checkpoint")
    ap.add_argument("--flag-authors", action="append", default=[], metavar="[LABEL:]NAME,NAME",
                    help="tag rows by these authors in author_flags (repeatable; default label 'flagged'). Explicit only: RSS has no flair data")
    return ap


def build_client(delay: float = 1.0) -> RedditRSSClient:
    creds = load_credentials()
    pacer = FileLockPacer(state_path_for(creds.token), interval=delay)   # shared across processes on this token
    return RedditRSSClient(RateLimitedTransport(creds, pacer=pacer))


def run_crawl_command(args: argparse.Namespace, client: RedditRSSClient) -> int:
    resume = read_resume_state(args.rows) if args.resume else None
    if resume and resume.done_post_ids:
        print(f"resuming: {len(resume.done_post_ids)} posts already in {args.rows}, continuing after {resume.last_post_id}", file=sys.stderr)
    appender = JsonlAppender(args.rows, resume=bool(args.resume))
    try:
        summary = SubredditCrawler(client, appender, AuthorFlagger.from_specs(args.flag_authors),
                                   StderrCrawlReporter(every=args.progress)).crawl(
            args.full_subreddit, args.listing_sort, args.max_posts, resume)
    finally:
        appender.close()
    summary_path = re.sub(r"\.jsonl?$", "", str(args.rows)) + ".crawl.json"
    Path(summary_path).write_text(json.dumps(summary.to_dict(), indent=2))
    print(f"rows -> {args.rows}   summary -> {summary_path}")
    print(f"posts fetched: {summary.posts_fetched}, comments: {summary.comments_total}, rows: {summary.rows_written}, "
          f"fetch errors: {summary.fetch_errors}, ceiling suspected: {summary.ceiling_suspected}")
    return 0


def run_batch_command(args: argparse.Namespace, client: RedditRSSClient) -> int:
    subs = [x.strip() for x in args.subs.split(",") if x.strip()]
    options = BatchOptions(limit=args.limit, sort=args.sort, time_filter=args.time, delay=args.delay,
                           thread_context=args.thread_context, scope=args.scope, breaker_after=args.breaker,
                           progress_every=args.progress, per_sub=args.per_sub)
    result = BatchRunner(client, load_terms(args.terms), options, StderrReporter(),
                         flagger=AuthorFlagger.from_specs(args.flag_authors)).run(subs)
    sink = JsonlFileSink(args.rows)
    sink.write_rows(result.rows)
    sink.write_stats(result.stats)

    stats, summary = result.stats, result.stats.summary()
    print(f"rows: {len(result.rows)} -> {sink.rows_path}")
    print(f"candidates: {summary['candidates_evaluated']} (posts {summary['unique_posts']} + comments "
          f"{summary['comments_total']}), kept strict: {summary['rows_kept_strict']}, thread_context: "
          f"{summary['rows_thread_context']}, discard rate: {summary['discard_rate_strict']}")
    print(f"discards by reason: {stats.discarded}")
    print("per-term verdicts:")
    for key, ts in stats.per_search.items():
        tripped = f" (tripped after {ts.breaker_tripped_after_posts} posts)" if ts.breaker_tripped else ""
        print(f"  {ts.verdict:24s} {key:34s} hits={ts.search_hits} post_precision={ts.post_level_precision} "
              f"posts_kept={ts.posts_kept} comments_kept={ts.comments_kept}/{ts.comments_fetched}{tripped}")
    print(f"stats -> {sink.stats_path}")
    return 0


def run_simple_command(args: argparse.Namespace, client: RedditRSSClient, parser: argparse.ArgumentParser) -> int:
    results: list[dict] = []
    if args.post:
        post, comments = client.comments(args.post)
        results.append({"post": post.to_dict() if post else None, "comments": [c.to_dict() for c in comments]})
    else:
        if not (args.subreddit and args.query):
            parser.error("need subreddit and query, or --post, or --subs + --terms")
        for post in client.search(args.subreddit, args.query, args.limit, args.sort, args.time):
            print(f"{post.id}  {post.title[:80]}")
            full, comments = client.comments(post.url or "")
            results.append({"post": (full or post).to_dict(), "comments": [c.to_dict() for c in comments]})

    for r in results:
        p = r["post"] or {}
        print(f"\n=== {p.get('title')}  by u/{p.get('author')}  ({len(r['comments'])} comments)")
        print((p.get("body") or "")[:400])
        for c in r["comments"]:
            print(f"  - u/{c['author']}: {c['body'][:120]!r}")
    if args.out:
        write_simple_results(args.out, results)
        print(f"\nwrote {args.out}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        client = build_client(args.delay)
    except MissingCredentials as e:
        print(f"missing credentials: {e}", file=sys.stderr)
        return 2
    if args.full_subreddit:
        return run_crawl_command(args, client)
    if args.subs and args.terms:
        return run_batch_command(args, client)
    return run_simple_command(args, client, parser)
