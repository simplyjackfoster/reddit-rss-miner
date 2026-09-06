"""reddit-rss-miner: search subreddits for product mentions via authenticated Reddit RSS feeds.

Layers (each depends only on the ones above it):
    config     credentials                 transport  polite HTTP with 429 backoff
    feed       Atom parsing, HTML utils     client     Reddit URL shapes: search(), comments()
    terms      product terms + matcher      batch      runner, breaker, stats, reporting
    writers    JSONL/JSON sinks             cli        argparse + composition root

Compatibility aliases for code written against the single-file version are kept below.
"""
from .batch import (BatchOptions, BatchResult, BatchRunner, BatchStats, CircuitBreaker, NullReporter, Reporter,
                    Row, StderrReporter, TermStats, run_batch)
from .client import CommentFetcher, PostSearcher, RedditRSSClient, normalize_post_path
from .config import Credentials, MissingCredentials, load_credentials
from .feed import Entry, extract_media, parse_entries, strip_html
from .terms import MatchResult, Term, load_terms, match_term
from .transport import HttpError, RateLimitedTransport, Transport, redact
from .writers import InMemorySink, JsonlFileSink, RowSink

__version__ = "0.2.0"

RedditRSS = RedditRSSClient       # legacy name


def load_env() -> Credentials:    # legacy name
    return load_credentials()


__all__ = [
    "BatchOptions", "BatchResult", "BatchRunner", "BatchStats", "CircuitBreaker", "NullReporter", "Reporter",
    "Row", "StderrReporter", "TermStats", "run_batch", "CommentFetcher", "PostSearcher", "RedditRSSClient",
    "RedditRSS", "normalize_post_path", "Credentials", "MissingCredentials", "load_credentials", "load_env",
    "Entry", "extract_media", "parse_entries", "strip_html", "MatchResult", "Term", "load_terms", "match_term",
    "RateLimitedTransport", "Transport", "HttpError", "redact", "InMemorySink", "JsonlFileSink", "RowSink",
]
