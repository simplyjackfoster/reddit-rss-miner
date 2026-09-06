"""Command line: argument parsing and the composition root where concrete classes are wired."""
from __future__ import annotations

import argparse
import sys
from typing import Sequence

from .batch import BatchOptions, BatchRunner, StderrReporter
from .client import RedditRSSClient
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
    return ap


def build_client(delay: float = 1.0) -> RedditRSSClient:
    creds = load_credentials()
    pacer = FileLockPacer(state_path_for(creds.token), interval=delay)   # shared across processes on this token
    return RedditRSSClient(RateLimitedTransport(creds, pacer=pacer))


def run_batch_command(args: argparse.Namespace, client: RedditRSSClient) -> int:
    subs = [x.strip() for x in args.subs.split(",") if x.strip()]
    options = BatchOptions(limit=args.limit, sort=args.sort, time_filter=args.time, delay=args.delay,
                           thread_context=args.thread_context, scope=args.scope, breaker_after=args.breaker,
                           progress_every=args.progress, per_sub=args.per_sub)
    result = BatchRunner(client, load_terms(args.terms), options, StderrReporter()).run(subs)
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
    if args.subs and args.terms:
        return run_batch_command(args, client)
    return run_simple_command(args, client, parser)
