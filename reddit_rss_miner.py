#!/usr/bin/env python3
"""
reddit-rss-miner: search subreddits for product mentions and pull matching posts + comments
=========================================================================================

WHY RSS AND NOT THE API
  - Reddit's anonymous .json endpoints return a "blocked by network security" 403 from
    residential IPs, and anonymous .rss gets one request then rolling 429s.
  - Reddit no longer approves new script-type OAuth apps for us.
  - The classic RSS feeds (search.rss, thread .rss) DO work when authenticated with the
    account's private feed token from https://www.reddit.com/prefs/feeds. That is the
    sanctioned "RSS reader" auth path: append ?feed=TOKEN&user=USERNAME to any .rss URL.
  - Tested 2026-09-05: 14 consecutive authed requests at 1/sec, zero 429s.
    A 352-comment thread returned 347 comments (gap = deleted comments), so limit=500 is fine.
  - .json variants stay 403 even with the token. Only .rss works.

CREDENTIALS (never committed; see .env.example)
  - Copy .env.example to .env and fill REDDIT_FEED_TOKEN / REDDIT_FEED_USER from
    https://www.reddit.com/prefs/feeds (open any RSS link there; the feed= and user= params).
  - The token grants read access to that account's private feeds (front page, saved, upvoted,
    inbox). Changing the account password invalidates it immediately (kill switch).

WHAT YOU GET
  - Posts:    id (t3_xxx), title, author, url, updated, body (HTML stripped), links, images, videos
  - Comments: id (t1_xxx), author, url (permalink), updated, body, links, images, videos
  - Comments are FLAT. RSS carries no score, no parent id, no depth, no total count.
  - search.rss returns max 100 per page; the script paginates with &after=<last t3 id>
    (verified: --limit 110 -> 110 unique ids).

BATCH MODE (subreddits x products, filtered, with disclosed discard stats)
  python3 reddit_rss_miner.py --subs productivity,macapps --terms terms.json --limit 100 --rows rows.jsonl
  - terms.json defines each product: aliases, strong_aliases, require, exclude, search (format in
    the BATCH MODE section further down). Start from the terms.json shipped alongside this file.
  - Output rows.jsonl: one line per matched item with sub, post_id, post_title, item_id, item_type
    (post|comment), author, body, url, updated, matched_terms, matched_in (post|comment|thread_context), snippet.
  - rows.stats.json: per (subs, term) search_hits / posts_kept / comments_fetched / comments_kept, plus
    discards by reason (no_mention, excluded_pattern, missing_required_context) and the overall
    discard rate. Report that number; do not hand-clean the rows.
  - Megathreads: a post is a row only if its OWN title/body names the product; a comment only if the
    comment does. So "Daily Question Thread" yields only the comments that mention the product.
  - --thread-context additionally keeps every comment under a matched post, tagged thread_context.
  - --scope title|selftext pre-filters server-side with Reddit's title:/selftext: operators.
  - Data integrity: a search returning 0 hits is retried once after 8s (Reddit occasionally returns
    a transient empty feed). Each term gets a `verdict` in stats:
      ok                       rows were produced
      confirmed_zero_presence  0 search hits on both attempts (the product is not discussed)
      hits_but_no_matches      Reddit matched posts but our aliases never did -> query too generic
                               or aliases wrong; check post_level_precision and fix terms.json
      breaker_tripped          same as above, detected early: after --breaker posts (default 12)
                               with 0 rows, the term's remaining posts are NOT fetched
    post_level_precision (share of search hits whose title/body names the product) is computed
    before any comment fetch and is the cheapest signal of query quality.
  - Progress goes to stderr every --progress posts (default 10) with rows, fetches, elapsed, ETA.
  - When the breaker trips, discard_rate covers fetched items only; see posts_skipped_by_breaker.
  - A failed comment fetch (5xx, timeout, malformed feed) is logged, recorded in stats.failed_posts
    and fetch_errors, and the run continues. Re-run those post ids with --post if they matter.
  - --subs a,b runs ONE pooled search per product (r/a+b), so --limit 100 is 100 posts total across
    the listed subs. Add --per-sub for one search per (sub, term) with the limit applied per sub;
    stats keys then read "<sub> :: <term>". Every row carries `sub` either way.
  - Rows also carry `links` (outbound URLs the author wrote, or the target of a link post) and
    `images`. Reddit-internal anchors (user pages, the thread itself) are stripped.
  - Stats granularity: discards-by-reason count (item, term) pairs; discard_rate is per item.
    They only sum exactly when no post is hit by more than one term.
  - Search operators verified on search.rss: quoted phrases, title:, selftext:, r/a+b multi-sub,
    and boolean OR (changes the result set: 21/25 overlap vs plain query).
  - Measured on a two-product test run (one distinctive name, one common-word name), 15 posts/term:
    6,474 candidates, 1,289 kept (80% discard; the common-word product alone kept 7 of 3,430 = 99.8%).
    Audit of that product: every English-word collision dropped, every real mention kept once
    strong_aliases and context words were configured.

USAGE
  pip install requests
  python3 reddit_rss_miner.py productivity '"things 3"' --limit 5 --out out.json
  python3 reddit_rss_miner.py productivity obsidian --sort new --time month
  python3 reddit_rss_miner.py --post r/productivity/comments/qpfrdk
  python3 reddit_rss_miner.py --post https://www.reddit.com/r/productivity/comments/qpfrdk/

  Search sort: relevance | hot | top | new | comments      Time: hour|day|week|month|year|all
  --delay N   seconds between requests (default 1.0). Keep it >= 1 to stay clear of 429s.
  --out FILE  JSON: [{"post": {...}, "comments": [{...}, ...]}, ...]

PROGRAMMATIC USE
  from reddit_rss_search_comments import RedditRSS, load_env
  rd = RedditRSS(load_env())
  for p in rd.search("productivity", '"things 3"', limit=20):        # generator of post dicts
      post, comments = rd.comments(p["url"])                  # (post dict, [comment dicts])
"""
import argparse, html, json, os, re, sys, time
import xml.etree.ElementTree as ET
import requests

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:155.0) Gecko/20100101 Firefox/155.0"
NS = {"a": "http://www.w3.org/2005/Atom"}
BASE = "https://www.reddit.com"


# Credentials are never stored in this file. Resolution order:
#   1. environment variables REDDIT_FEED_TOKEN / REDDIT_FEED_USER
#   2. ./.env in the working directory
#   3. ~/.reddit_feed.env
ENV_FILES = [".env", os.path.expanduser("~/.reddit_feed.env")]


def load_env():
    for path in ENV_FILES:
        if os.path.exists(path):
            for line in open(path):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    tok, user = os.environ.get("REDDIT_FEED_TOKEN"), os.environ.get("REDDIT_FEED_USER")
    if not (tok and user):
        sys.exit("missing credentials: set REDDIT_FEED_TOKEN and REDDIT_FEED_USER (env, ./.env, or ~/.reddit_feed.env). "
                 "Get the token from https://www.reddit.com/prefs/feeds -> any RSS link -> feed= and user= params.")
    return {"feed": tok, "user": user}


_REDDIT_INTERNAL = re.compile(r"^https?://(?:www\.|old\.)?reddit\.com/(?:user/|u/|message/|r/[^/]+/?$|r/[^/]+/comments/)", re.I)
_VIDEO_HOSTS = ("youtube.com", "youtu.be", "v.redd.it", "vimeo.com", "streamable.com", "redgifs.com")


def extract_media(content_html: str) -> dict:
    """Outbound links, images and videos from an entry's content HTML.

    Reddit's feed content wraps the post body plus boilerplate anchors ("submitted by /u/x",
    "[link]", "[comments]"). Reddit-internal anchors (user pages, the thread itself, subreddit roots)
    are dropped, so for a link post the "[link]" anchor yields the external URL, and for a self post
    or comment only the URLs the author actually wrote survive. Idea ported from
    sametcn99/reddit-rss-api (extracters.ts), re-implemented with stdlib regex.
    """
    hrefs = [html.unescape(h) for h in re.findall(r'<a\s[^>]*?href="([^"]+)"', content_html or "", re.I)]
    images = [html.unescape(u) for u in re.findall(r'<img\s[^>]*?src="([^"]+)"', content_html or "", re.I)]
    links, videos, seen = [], [], set(images)
    for h in hrefs:
        if h in seen or _REDDIT_INTERNAL.match(h) or not re.match(r"https?://", h, re.I):
            continue  # relative paths (/r/x, /message/compose) and anchors are Reddit-internal
        seen.add(h)
        (videos if any(v in h for v in _VIDEO_HOSTS) else links).append(h)
    return {"links": links, "images": images, "videos": videos}


def strip_html(s: str) -> str:
    s = re.sub(r"<!--.*?-->", "", s or "", flags=re.S)
    s = re.sub(r"<br\s*/?>|</p>|</li>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


class RedditRSS:
    def __init__(self, auth: dict):
        self.auth = auth
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA

    def fetch(self, path: str, **params):
        params.update(self.auth)
        for attempt in range(5):
            r = self.s.get(f"{BASE}{path}", params=params, timeout=30)
            if r.status_code == 429:
                wait = float(r.headers.get("X-Ratelimit-Reset", 30)) + 1
                print(f"429, sleeping {wait:.0f}s", file=sys.stderr)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return ET.fromstring(r.content)
        raise RuntimeError("gave up after repeated 429s")

    @staticmethod
    def entries(root):
        for e in root.findall("a:entry", NS):
            au = e.find("a:author/a:name", NS)
            link = e.find("a:link", NS)
            content = e.findtext("a:content", default="", namespaces=NS)
            yield {
                "id": e.findtext("a:id", default="", namespaces=NS),
                "title": e.findtext("a:title", default="", namespaces=NS),
                "author": (au.text if au is not None else "").removeprefix("/u/") or None,
                "url": link.get("href") if link is not None else None,
                "updated": e.findtext("a:updated", default="", namespaces=NS),
                "body": strip_html(content),
                **extract_media(content),
            }

    # ---- search ----
    def search(self, subreddit, query, limit=25, sort="relevance", t="all"):
        after, got = None, 0
        while got < limit:
            root = self.fetch(f"/r/{subreddit}/search.rss", q=query, restrict_sr=1, sort=sort, t=t,
                              limit=min(100, limit - got), after=after)
            page = list(self.entries(root))
            if not page:
                return
            for p in page:
                yield p
                got += 1
                if got >= limit:
                    return
            after = page[-1]["id"]          # t3_xxxxx

    # ---- comments ----
    def comments(self, post_url_or_path: str):
        path = post_url_or_path.replace(BASE, "")
        path = "/" + path.strip("/")
        path = re.sub(r"/comments/([a-z0-9]+)(/[^/]*)?/?$", r"/comments/\1/", path)
        root = self.fetch(f"{path}.rss", limit=500, depth=10, sort="confidence")
        items = list(self.entries(root))
        post = next((i for i in items if i["id"].startswith("t3_")), None)
        comments = [i for i in items if i["id"].startswith("t1_")]
        return post, comments


# =====================================================================================
# BATCH MODE: subreddits x product terms, with per-item relevance filtering + stats
# =====================================================================================
#
# Why: plain Reddit search matches COMMENT text too. A 500-comment "Daily Question Thread"
# becomes a search hit because one comment names the product, and then every comment in it
# looks like a row. Common-word product names ("Things") collide with ordinary English.
# Fix: a post is a row only if its own title/body mentions the term; comments are rows only
# if the comment itself mentions the term; every discard is counted and written to the stats.
#
# terms.json format (one entry per product):
#   {
#     "Things": {
#       "aliases": ["things"],
#       "strong_aliases": ["things 3", "culturedcode.com", "things app"],
#       "search": "\"things 3\" OR \"things app\"",              # optional Reddit query; default "<Name>"
#       "require": ["app", "task", "tasks", "todo", "mac", "ios"],
#       "exclude": ["things to\b", "\bmany things", "things\s*!"],
#       "case_sensitive": false
#     },
#     "Obsidian": {"aliases": ["obsidian"], "search": "\"obsidian\""}
#   }
#   aliases        word-boundary matched in item text; any alias hit is a "mention"
#   strong_aliases unambiguous forms (domains, product+noun) that satisfy `require` on their own
#   require        if present, at least one must also appear in the same item (context words)
#   exclude        regexes; a mention inside an exclude match does not count
#   case_sensitive default false. Weak heuristic on Reddit (people lowercase everything);
#                  prefer require/exclude.
#   search         query sent to Reddit; defaults to the quoted term name. Quoted phrases ARE
#                  honored by search.rss (tested: a two-word name quoted -> 99/100 exact matches vs 40/100 unquoted).
#                  title:/selftext: operators also work (see --scope).
#
# Strict vs thread-context: by default a comment under a matched post that does not itself
# name the product is dropped (counted as no_mention). --thread-context keeps those, tagged
# matched_in="thread_context", so downstream can separate them.
#
# Budget: each kept-or-not post costs one comment fetch. 14 searches x 100 posts at 1/s is
# ~25 min worst case; posts hit by several terms are fetched once.

def load_terms(path):
    raw = json.load(open(path))
    terms = {}
    for name, cfg in raw.items():
        if isinstance(cfg, list):
            cfg = {"aliases": cfg}
        aliases = cfg.get("aliases") or [name]
        flags = 0 if cfg.get("case_sensitive") else re.IGNORECASE
        strong = cfg.get("strong_aliases", [])
        terms[name] = {
            "alias_re": re.compile(r"(?<!\w)(?:" + "|".join(re.escape(a) for a in aliases + strong) + r")(?!\w)", flags),
            "strong_re": (re.compile(r"(?<!\w)(?:" + "|".join(re.escape(a) for a in strong) + r")(?!\w)", flags)
                          if strong else None),
            "require_re": (re.compile(r"(?<!\w)(?:" + "|".join(re.escape(w) for w in cfg["require"]) + r")(?!\w)", re.I)
                           if cfg.get("require") else None),
            "exclude_res": [re.compile(x, re.I) for x in cfg.get("exclude", [])],
            "search": cfg.get("search") or f'"{name}"',
        }
    return terms


def match_term(text, t):
    """Returns (matched, reason, snippet). reason in: ok, no_mention, excluded_pattern, missing_required_context."""
    mentions = list(t["alias_re"].finditer(text))
    if not mentions:
        return False, "no_mention", None
    excluded_spans = [m.span() for x in t["exclude_res"] for m in x.finditer(text)]
    good = [m for m in mentions if not any(a <= m.start() < b for a, b in excluded_spans)]
    if not good:
        return False, "excluded_pattern", None
    if t["require_re"] and not t["require_re"].search(text) and not (t["strong_re"] and t["strong_re"].search(text)):
        return False, "missing_required_context", None
    m = good[0]
    lo, hi = max(0, m.start() - 80), min(len(text), m.end() + 80)
    return True, "ok", re.sub(r"\s+", " ", text[lo:hi]).strip()


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def run_batch(rd, subs, terms, limit, sort, t, delay, thread_context=False, scope="all",
              breaker_after=12, progress_every=10, zero_retry_delay=8.0, per_sub=False):
    """Returns (rows, stats). See header for row schema and the per-term verdict field.

    Data-integrity safeguards:
      - A search that returns 0 hits is retried once after `zero_retry_delay` seconds. Only a
        repeated 0 is reported as verdict "confirmed_zero_presence"; a recovered one is logged.
      - Circuit breaker: after `breaker_after` posts have been fully evaluated for a term with
        zero post rows and zero comment rows, the term trips. Remaining posts hit ONLY by
        tripped terms are not fetched (counted in posts_skipped_by_breaker). Set 0 to disable.
      - Per-term verdict in stats: ok | confirmed_zero_presence | hits_but_no_matches (query too
        generic or aliases wrong) | breaker_tripped.
      - post_level_precision per term is computed before any comment fetch: share of search hits
        whose own title/body names the product. Free early signal of query quality.
      - per_sub=True runs one search per (subreddit, term) instead of one pooled r/a+b search per
        term, so `limit` applies per subreddit. Stats keys become "<sub> :: <term>".
    """
    t0 = time.time()
    stats = {"per_search": {}, "discarded": {"no_mention": 0, "excluded_pattern": 0, "missing_required_context": 0},
             "posts_seen": 0, "posts_deduped": 0, "comment_fetches": 0, "comments_total": 0,
             "posts_skipped_by_breaker": 0, "fetch_errors": 0, "failed_posts": [],
             "rows": {"post": 0, "comment": 0, "thread_context": 0}}
    rows = []
    groups = list(subs) if per_sub else ["+".join(subs)]
    posts = {}       # post_id -> {"post": p, "hits": {(group, term)}}
    order = []       # post ids in first-seen order

    def key(group, name):
        return f"{group} :: {name}"

    # ---- phase 1: searches (with zero-hit retry) ----
    jobs = [(g, n, c) for g in groups for n, c in terms.items()]
    for i, (group, name, tcfg) in enumerate(jobs, 1):
        q = tcfg["search"]
        if scope in ("title", "selftext"):
            q = f"{scope}:{q}"
        attempts, hits_list = 0, []
        while True:
            attempts += 1
            hits_list = list(rd.search(group, q, limit=limit, sort=sort, t=t))
            if hits_list or attempts >= 2:
                break
            _log(f"[search {i}/{len(jobs)}] {group} :: {name}: 0 hits, retrying once in {zero_retry_delay:.0f}s")
            time.sleep(zero_retry_delay)
        ps = {"search_hits": len(hits_list), "search_attempts": attempts,
              "zero_hits_recovered_on_retry": bool(hits_list) and attempts > 1,
              "post_level_hits": 0, "post_level_precision": None,
              "posts_evaluated": 0, "posts_kept": 0, "comments_fetched": 0, "comments_kept": 0,
              "breaker_tripped": False, "verdict": None}
        for p in hits_list:
            stats["posts_seen"] += 1
            if p["id"] in posts:
                stats["posts_deduped"] += 1
            else:
                order.append(p["id"])
            posts.setdefault(p["id"], {"post": p, "hits": set()})["hits"].add((group, name))
            if match_term(f"{p['title']}\n{p['body']}", tcfg)[0]:
                ps["post_level_hits"] += 1
        if hits_list:
            ps["post_level_precision"] = round(ps["post_level_hits"] / len(hits_list), 3)
        stats["per_search"][key(group, name)] = ps
        _log(f"[search {i}/{len(jobs)}] {group} :: {name}: {len(hits_list)} hits, "
             f"{ps['post_level_hits']} name the product in title/body"
             + (" (recovered after empty first response)" if ps["zero_hits_recovered_on_retry"] else ""))
        time.sleep(delay)

    # ---- phase 2: comments, with per-term circuit breaker ----
    tripped = set()
    for n, pid in enumerate(order, 1):
        entry = posts[pid]
        p = entry["post"]
        active = [(g, name) for (g, name) in entry["hits"] if (g, name) not in tripped]
        if not active:
            stats["posts_skipped_by_breaker"] += 1
            if progress_every and n == len(order):
                _log(f"[{n}/{len(order)} posts] rows {len(rows)} | fetches {stats['comment_fetches']} | "
                     f"skipped by breaker {stats['posts_skipped_by_breaker']} | done")
            continue
        sub = p["url"].split("/r/")[1].split("/")[0] if "/r/" in (p["url"] or "") else None
        post_terms = []
        for g, name in active:
            ok, reason, snip = match_term(f"{p['title']}\n{p['body']}", terms[name])
            if ok:
                if name not in [x for x, _ in post_terms]:
                    post_terms.append((name, snip))
                stats["per_search"][key(g, name)]["posts_kept"] += 1
            else:
                stats["discarded"][reason] += 1
        if post_terms:
            stats["rows"]["post"] += 1
            rows.append({"sub": sub, "post_id": pid, "post_title": p["title"], "item_id": pid, "item_type": "post",
                         "author": p["author"], "body": p["body"], "url": p["url"], "updated": p["updated"],
                         "links": p.get("links", []), "images": p.get("images", []),
                         "matched_terms": [x for x, _ in post_terms], "matched_in": "post", "snippet": post_terms[0][1]})

        time.sleep(delay)
        try:
            _, comments = rd.comments(p["url"])
        except Exception as e:  # 5xx, timeout, malformed feed: log, record, keep going
            stats["fetch_errors"] += 1
            stats["failed_posts"].append({"post_id": pid, "url": p["url"], "error": f"{type(e).__name__}: {e}"[:200]})
            _log(f"[fetch error] {pid} {type(e).__name__}: {str(e)[:120]} -> skipped, continuing")
            continue
        stats["comment_fetches"] += 1
        stats["comments_total"] += len(comments)
        for g, name in active:
            stats["per_search"][key(g, name)]["comments_fetched"] += len(comments)
        for c in comments:
            c_terms = []
            for g, name in active:
                ok, reason, snip = match_term(c["body"], terms[name])
                if ok:
                    if name not in [x for x, _ in c_terms]:
                        c_terms.append((name, snip))
                    stats["per_search"][key(g, name)]["comments_kept"] += 1
                else:
                    stats["discarded"][reason] += 1
            if c_terms:
                stats["rows"]["comment"] += 1
                kind, snip, mt = "comment", c_terms[0][1], [x for x, _ in c_terms]
            elif thread_context and post_terms:
                stats["rows"]["thread_context"] += 1
                kind, snip, mt = "thread_context", None, [x for x, _ in post_terms]
            else:
                continue
            rows.append({"sub": sub, "post_id": pid, "post_title": p["title"], "item_id": c["id"], "item_type": "comment",
                         "author": c["author"], "body": c["body"], "url": c["url"], "updated": c["updated"],
                         "links": c.get("links", []), "images": c.get("images", []),
                         "matched_terms": mt, "matched_in": kind, "snippet": snip})

        for g, name in active:
            ps = stats["per_search"][key(g, name)]
            ps["posts_evaluated"] += 1
            if (breaker_after and ps["posts_evaluated"] >= breaker_after
                    and ps["posts_kept"] == 0 and ps["comments_kept"] == 0):
                tripped.add((g, name))
                ps["breaker_tripped"] = True
                ps["breaker_tripped_after_posts"] = ps["posts_evaluated"]
                _log(f"[breaker] {g} :: {name}: 0 rows after {ps['posts_evaluated']} posts / "
                     f"{ps['comments_fetched']} comments. Skipping its remaining posts; query looks too generic.")

        if progress_every and (n % progress_every == 0 or n == len(order)):
            el = time.time() - t0
            done = n
            left = len(order) - n
            eta = (el / done) * left if done else 0
            _log(f"[{n}/{len(order)} posts] rows {len(rows)} | fetches {stats['comment_fetches']} | "
                 f"elapsed {int(el)//60}:{int(el)%60:02d} | eta ~{int(eta)//60}:{int(eta)%60:02d}")

    # ---- verdicts + summary ----
    for ps in stats["per_search"].values():
        if ps["search_hits"] == 0:
            ps["verdict"] = "confirmed_zero_presence"      # 0 hits on two attempts
        elif ps["breaker_tripped"]:
            ps["verdict"] = "breaker_tripped"              # hits but no matches in first N posts
        elif ps["posts_kept"] == 0 and ps["comments_kept"] == 0:
            ps["verdict"] = "hits_but_no_matches"          # Reddit matched, our aliases never did
        else:
            ps["verdict"] = "ok"

    unique_posts = stats["posts_seen"] - stats["posts_deduped"]
    kept = stats["rows"]["post"] + stats["rows"]["comment"]
    cand = unique_posts + stats["comments_total"]
    stats["summary"] = {"unique_posts": unique_posts, "comments_total": stats["comments_total"],
                        "candidates_evaluated": cand, "rows_kept_strict": kept,
                        "rows_thread_context": stats["rows"]["thread_context"],
                        "discard_rate_strict": round(1 - kept / cand, 4) if cand else None,
                        "posts_skipped_by_breaker": stats["posts_skipped_by_breaker"],
                        "fetch_errors": stats["fetch_errors"],
                        "elapsed_seconds": round(time.time() - t0, 1),
                        "verdicts": {k: v["verdict"] for k, v in stats["per_search"].items()}}
    return rows, stats


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("USAGE")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("subreddit", nargs="?")
    ap.add_argument("query", nargs="?")
    ap.add_argument("--limit", type=int, default=10, help="posts per search (paginates past 100)")
    ap.add_argument("--sort", default="relevance", choices=["relevance", "hot", "top", "new", "comments"])
    ap.add_argument("--time", default="all", choices=["hour", "day", "week", "month", "year", "all"])
    ap.add_argument("--post", help="skip search; fetch this post (URL or r/sub/comments/id)")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--out", help="simple mode: write [{post, comments}] JSON here")
    ap.add_argument("--subs", help="BATCH: comma-separated subreddits, e.g. productivity,macapps")
    ap.add_argument("--terms", help="BATCH: terms.json (format in file header)")
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
    a = ap.parse_args()

    rd = RedditRSS(load_env())

    if a.subs and a.terms:
        subs = [x.strip() for x in a.subs.split(",") if x.strip()]
        terms = load_terms(a.terms)
        rows, stats = run_batch(rd, subs, terms, a.limit, a.sort, a.time, a.delay, a.thread_context, a.scope,
                                breaker_after=a.breaker, progress_every=a.progress, per_sub=a.per_sub)
        with open(a.rows, "w") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        stats_path = re.sub(r"\.jsonl?$", "", a.rows) + ".stats.json"
        json.dump(stats, open(stats_path, "w"), indent=2)
        s = stats["summary"]
        print(f"rows: {len(rows)} -> {a.rows}")
        print(f"candidates: {s.get('candidates_evaluated')} (posts {s['unique_posts']} + comments {s['comments_total']}), "
              f"kept strict: {s['rows_kept_strict']}, thread_context: {s['rows_thread_context']}, "
              f"discard rate: {s.get('discard_rate_strict')}")
        print(f"discards by reason: {stats['discarded']}")
        print("per-term verdicts:")
        for k, v in stats["per_search"].items():
            print(f"  {v['verdict']:24s} {k:34s} hits={v['search_hits']} post_precision={v['post_level_precision']} "
                  f"posts_kept={v['posts_kept']} comments_kept={v['comments_kept']}/{v['comments_fetched']}"
                  + (f" (tripped after {v['breaker_tripped_after_posts']} posts)" if v["breaker_tripped"] else ""))
        print(f"stats -> {stats_path}")
        return

    results = []
    if a.post:
        post, comments = rd.comments(a.post)
        results.append({"post": post, "comments": comments})
    else:
        if not (a.subreddit and a.query):
            ap.error("need subreddit and query, or --post, or --subs + --terms")
        for p in rd.search(a.subreddit, a.query, a.limit, a.sort, a.time):
            print(f"{p['id']}  {p['title'][:80]}")
            time.sleep(a.delay)
            post, comments = rd.comments(p["url"])
            results.append({"post": post or p, "comments": comments})
            time.sleep(a.delay)

    for r in results:
        p = r["post"]
        print(f"\n=== {p['title']}  by u/{p['author']}  ({len(r['comments'])} comments)")
        print(p["body"][:400])
        for c in r["comments"]:
            print(f"  - u/{c['author']}: {c['body'][:120]!r}")

    if a.out:
        json.dump(results, open(a.out, "w"), indent=2)
        print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
