import json

from reddit_rss_miner import AuthorFlagger, Entry
from reddit_rss_miner.crawl import (CEILING_SUSPECT_AT, JsonlAppender, NullCrawlReporter, ResumeState, SubredditCrawler,
                                    read_resume_state)

from conftest import comment, post


class ListingFake:
    """`total` posts served newest-first in pages of `page`; comments() returns two comments per post."""

    def __init__(self, total, page=100, fail_ids=()):
        self.posts = [post(f"p{i:04d}", f"post {i}") for i in range(total)]
        self.page, self.fetched, self.fail_ids, self.pages_served = page, [], set(fail_ids), 0

    def listing_page(self, subreddit, sort="new", after=None, limit=100):
        self.pages_served += 1
        start = 0 if after is None else next(i for i, p in enumerate(self.posts) if p.id == after) + 1
        return self.posts[start:start + self.page]

    def comments(self, url):
        pid = url.split("/comments/")[1].split("/")[0]
        self.fetched.append(pid)
        if pid in self.fail_ids:
            raise RuntimeError(f"boom https://www.reddit.com/x.rss?feed=SECRET&user=U")
        return None, [comment(f"{pid}a", "first"), comment(f"{pid}b", "second")]


class MemAppender:
    def __init__(self): self.writes = []
    def append(self, rows): self.writes.append(rows)


def crawl(fake, appender=None, **kw):
    appender = appender or MemAppender()
    s = SubredditCrawler(fake, appender, kw.pop("flagger", None), NullCrawlReporter(), clock=lambda: 0.0).crawl("x", **kw)
    return s, appender


def test_rows_schema_unfiltered_and_one_write_per_post():
    fake = ListingFake(3)
    s, mem = crawl(fake, flagger=AuthorFlagger.from_specs(["official:b"]))
    assert s.posts_fetched == 3 and s.comments_total == 6 and s.rows_written == 9 and s.exhausted and not s.ceiling_suspected
    assert len(mem.writes) == 3 and [len(w) for w in mem.writes] == [3, 3, 3]
    row = mem.writes[0][1].to_dict()
    assert row["matched_in"] == "dedicated_subreddit" and row["matched_terms"] == [] and row["item_type"] == "comment"
    assert row["author_flags"] == ["official"] and mem.writes[0][0].to_dict()["author_flags"] == []
    assert list(row)[-1] == "author_flags"


def test_max_posts_stops_early():
    s, mem = crawl(ListingFake(50, page=10), max_posts=12)
    assert s.posts_fetched == 12 and s.stopped_at_max and not s.exhausted and len(mem.writes) == 12


def test_ceiling_suspected_when_exhausted_near_1000():
    s, _ = crawl(ListingFake(988))
    assert s.exhausted and s.ceiling_suspected and s.posts_listed_total == 988
    s2, _ = crawl(ListingFake(CEILING_SUSPECT_AT - 1))
    assert s2.exhausted and not s2.ceiling_suspected


def test_fetch_error_is_recorded_redacted_and_run_continues():
    s, mem = crawl(ListingFake(4, fail_ids={"p0001"}))
    assert s.fetch_errors == 1 and s.posts_fetched == 3 and len(mem.writes) == 3
    assert "SECRET" not in s.failed_posts[0]["error"] and "<redacted>" in s.failed_posts[0]["error"]


def test_resume_round_trip_through_jsonl(tmp_path):
    out = tmp_path / "crawl.jsonl"
    fake = ListingFake(30, page=10)
    app = JsonlAppender(out, resume=False)
    s1, _ = crawl(fake, app, max_posts=13)      # simulate a kill after 13 posts
    app.close()
    state = read_resume_state(out)
    assert len(state.done_post_ids) == 13 and state.last_post_id == "t3_p0012"

    fake2 = ListingFake(30, page=10)
    app2 = JsonlAppender(out, resume=True)
    s2, _ = crawl(fake2, app2, resume=state)
    app2.close()
    assert s2.posts_fetched == 17 and s2.posts_skipped_resume == 0, "pagination resumed after the last id, nothing refetched"
    assert fake2.fetched[0] == "p0013" and s2.exhausted
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    assert len(rows) == 90 and len({r["post_id"] for r in rows}) == 30


def test_max_posts_is_a_total_across_resume():
    state = ResumeState(frozenset({"t3_p0000", "t3_p0001", "t3_p0002"}), "t3_p0002")
    s, _ = crawl(ListingFake(30, page=10), max_posts=5, resume=state)
    assert s.posts_fetched == 2 and s.stopped_at_max


def test_resume_skips_posts_already_present_when_listing_overlaps():
    """If the cursor post vanished, listing restarts overlapping; already-written posts are skipped, not duplicated."""
    fake = ListingFake(6, page=3)
    state = ResumeState(frozenset({"t3_p0000", "t3_p0001"}), None)   # no cursor -> starts from the top
    s, mem = crawl(fake, resume=state)
    assert s.posts_skipped_resume == 2 and s.posts_fetched == 4 and fake.fetched[0] == "p0002"


def test_read_resume_state_ignores_partial_last_line(tmp_path):
    out = tmp_path / "c.jsonl"
    out.write_text('{"post_id": "a"}\n{"post_id": "a"}\n{"post_id": "b"}\n{"post_id": "c", "trunc')
    st = read_resume_state(out)
    assert st.done_post_ids == {"a", "b"} and st.last_post_id == "b"
    assert read_resume_state(tmp_path / "missing.jsonl") == ResumeState(frozenset(), None)
