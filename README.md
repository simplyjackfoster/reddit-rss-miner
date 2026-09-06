# reddit-rss-miner

Search subreddits for product mentions and pull the matching posts and comments into a clean
JSONL file, with the filtering stats disclosed alongside. Uses Reddit's classic RSS feeds
authenticated with a private feed token. No OAuth app, no browser automation.

## Why RSS

- Reddit's anonymous `.json` endpoints return a "blocked by network security" 403 from
  residential IPs, and anonymous `.rss` gets one request and then rolling 429s.
- New script-type OAuth apps are not being approved.
- The classic RSS feeds (`search.rss`, thread `.rss`) work when authenticated with the account's
  private feed token from <https://www.reddit.com/prefs/feeds>. That is Reddit's sanctioned auth
  path for RSS readers: append `?feed=TOKEN&user=USERNAME` to any `.rss` URL.
- Measured: 14 consecutive authenticated requests at one per second with zero 429s. A
  352-comment thread returned 347 comments (the gap is deleted comments).

## Setup

```bash
pip install -e .          # installs the `reddit-rss-miner` command; or: pip install -r requirements.txt
cp .env.example .env      # fill REDDIT_FEED_TOKEN and REDDIT_FEED_USER
cp terms.example.json terms.json
reddit-rss-miner --post r/productivity/comments/qpfrdk   # smoke test: one post, 14 comments
```

Without installing, every command below also works as `python3 -m reddit_rss_miner ...`.

Get the token from <https://www.reddit.com/prefs/feeds>: open any RSS link on that page and copy
the `feed=` and `user=` query parameters. Credentials resolve from environment variables, then
`./.env`, then `~/.reddit_feed.env`. They are never stored in the script.

**The token grants read access to that account's private feeds, including inbox and saved posts.**
Never commit `.env`. Changing the account password invalidates the token immediately.

## Usage

Simple mode: one subreddit, one query, every comment dumped.

```bash
reddit-rss-miner productivity '"things 3"' --limit 5 --out out.json
```

Batch mode: subreddits × products, filtered for relevance, with stats.

```bash
reddit-rss-miner --subs productivity,macapps --terms terms.json --limit 100 --rows rows.jsonl
```

Writes `rows.jsonl` (one line per matched post or comment) and `rows.stats.json`.

### Row schema

`sub, post_id, post_title, item_id, item_type (post|comment), author, body, url, updated,
links, images, matched_terms, matched_in (post|comment|thread_context), snippet`

`links` holds outbound URLs the author wrote, or the target of a link post. `images` holds `<img>`
sources. Reddit-internal anchors (user pages, the thread itself, relative paths) are stripped.
Simple-mode posts and comments also carry `videos` (YouTube, v.redd.it, Vimeo, Streamable).

### Why the filter exists

Reddit search matches comment text, so a 500-comment "Daily Question Thread" becomes a hit when
one comment names your product, and every comment in it looks like data. Product names also
collide with ordinary English ("Things" is a task manager and a word people say). A naive pass
without this filter produced 6,800 rows that were 98% noise.

Rule: a post is a row only if its own title or body names the product. A comment is a row only if
the comment itself does. Every discard is counted in the stats file by reason
(`no_mention`, `excluded_pattern`, `missing_required_context`). Report the discard rate from the
stats file; do not hand-clean rows.

### terms.json

One entry per product. See `terms.example.json`.

| key | meaning |
|---|---|
| `search` | query sent to Reddit. Quote multi-word names. `title:`, `selftext:`, OR, and phrases are honored. Defaults to `"<Name>"`. |
| `aliases` | how the product appears in text, matched on word boundaries |
| `strong_aliases` | unambiguous forms (domain, "things app") that count without context words |
| `require` | context words, one of which must co-occur for a plain alias to count |
| `exclude` | regexes for false-positive phrases such as `things to\b` |
| `case_sensitive` | default false. Weak heuristic on Reddit; prefer `require`/`exclude`. |

A distinctive name (Obsidian) needs only `search` and `aliases`. A common-word name (Things) needs all of them.

### Data-integrity safeguards

- A search returning zero hits is retried once after 8 s. Stats record `search_attempts` and
  `zero_hits_recovered_on_retry`.
- Each term gets a `verdict`: `ok`, `confirmed_zero_presence` (0 hits on both attempts),
  `hits_but_no_matches` (Reddit matched posts but the aliases never did: fix the query),
  `breaker_tripped` (same, caught early).
- Circuit breaker: after `--breaker` fully evaluated posts with zero rows (default 12, `0` disables),
  the term trips and its remaining posts are not fetched. Posts shared with an active term are still
  fetched. See `posts_skipped_by_breaker`. When tripped, `discard_rate` covers fetched items only.
- `post_level_precision` per term (share of search hits whose title/body names the product) is
  computed before any comment fetch. Cheapest signal of query quality.
- A failed comment fetch (5xx, timeout, malformed feed) is logged and recorded in `failed_posts`;
  the run continues. Re-run those ids with `--post`.
- Progress to stderr every `--progress` posts (default 10): rows, fetches, elapsed, ETA.

### Things that bite

- `--limit` is total across the subs listed (one pooled `r/a+b` search per product). Add `--per-sub`
  to run one search per (subreddit, term) with the limit applied per sub; stats keys then read
  `<sub> :: <term>`. Every row carries `sub` either way.
- Each post costs one request for its comments at `--delay` seconds apart (default 1). Ten products
  at 100 posts each is roughly 20 minutes worst case; keep the delay at 1 or higher.
- Strict filtering is the default. `--thread-context` also keeps non-matching comments under
  matched posts, tagged `thread_context`.
- RSS carries no score, parent id, depth, or total comment count. Comments are flat.
- Stats count discards per (item, term) pair but the discard rate per item; they only sum exactly
  when no post is hit by more than one term.

## Programmatic use

```python
from reddit_rss_miner import (BatchOptions, BatchRunner, RateLimitedTransport, RedditRSSClient,
                              StderrReporter, load_credentials, load_terms)

client = RedditRSSClient(RateLimitedTransport(load_credentials()))
for post in client.search("productivity", '"things 3"', limit=20):     # Entry objects
    post, comments = client.comments(post.url)

result = BatchRunner(client, load_terms("terms.json"), BatchOptions(limit=100), StderrReporter()).run(["productivity", "macapps"])
result.rows            # list[Row]      -> row.to_dict()
result.stats           # BatchStats     -> stats.to_dict(), stats.summary()
```

`run_batch(...)`, `RedditRSS`, `load_env` and `match_term` are kept as aliases for code written
against the earlier single-file version; `run_batch` returns plain dicts.

## Architecture

```
reddit_rss_miner/
  config.py     Credentials + load_credentials()      env > ./.env > ~/.reddit_feed.env; never in code
  transport.py  RateLimitedTransport (Transport)      UA, auth params, 429 backoff, bounded retries
  feed.py       Entry, parse_entries, strip_html,     Atom parsing and content-HTML utilities; no network
                extract_media
  client.py     RedditRSSClient (PostSearcher,        Reddit URL shapes: search.rss pagination, thread .rss
                CommentFetcher)
  terms.py      Term, MatchResult, load_terms         product definitions and the per-item matcher
  batch.py      BatchOptions, BatchRunner,            search phase (zero-hit retry), comment phase
                CircuitBreaker, Row/TermStats/         (breaker, fetch-error tolerance), verdicts,
                BatchStats, Reporter                   progress reporting via a Reporter protocol
  writers.py    JsonlFileSink, InMemorySink (RowSink)  output; the runner never touches the filesystem
  cli.py        build_parser(), main()                 argument parsing and the composition root
```

Each module has one job and depends only on the layers above it. The runner takes any object
satisfying `PostSearcher` + `CommentFetcher`, any `Reporter`, and injectable `sleep`/`clock`, so the
whole batch pipeline is tested offline with a fake client and no monkeypatching. Concrete classes are
wired together only in `cli.py`.

## Credits

Outbound link/image/video extraction from feed HTML and the per-subreddit fetch option were adapted
from ideas in [sametcn99/reddit-rss-api](https://github.com/sametcn99/reddit-rss-api), a Deno
service that serves subreddit listing feeds as JSON. Re-implemented here in stdlib Python.

## Tests

Offline, no network:

```bash
python3 -m pytest
```

Modules are tested individually (`tests/test_<module>.py`); `tests/conftest.py` holds the shared
fake client scenario. The batch tests assert the exact row schema and `stats.json` key layout, since
downstream consumers depend on them.
