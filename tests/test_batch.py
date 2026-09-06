from reddit_rss_miner import BatchOptions, BatchRunner, CircuitBreaker, InMemorySink, NullReporter, TermStats, run_batch
from reddit_rss_miner.writers import JsonlFileSink

from conftest import NO_SLEEP, FakeClient


def run(fake, terms, subs=("x",), **opts):
    o = BatchOptions(limit=50, delay=0, progress_every=0, **opts)
    return BatchRunner(fake, terms, o, NullReporter(), sleep=NO_SLEEP, clock=lambda: 0.0).run(list(subs))


def by_term(stats):
    return {k.split(" :: ")[1]: v for k, v in stats.per_search.items()}


def test_retry_breaker_verdicts_and_megathread(fake, terms):
    res = run(fake, terms, breaker_after=5)
    ps = by_term(res.stats)
    assert all(k.startswith("x :: ") for k in res.stats.per_search)
    assert ps["Things"].search_attempts == 2 and ps["Things"].zero_hits_recovered_on_retry and ps["Things"].search_hits == 3
    assert ps["Tana"].verdict == "confirmed_zero_presence" and ps["Tana"].search_attempts == 2
    assert ps["Craft"].verdict == "breaker_tripped" and ps["Craft"].breaker_tripped_after_posts == 5
    assert res.stats.posts_skipped_by_breaker >= 10
    assert any("/shared/" in u for u in fake.fetched), "post shared with an active term must still be fetched"
    assert ps["Things"].verdict == "ok" and ps["Things"].comments_kept == 1
    assert ps["Craft"].post_level_precision == 0.0 and ps["Obsidian"].post_level_precision == 1.0
    assert not any(r.post_id == "t3_k2" and r.item_type == "post" for r in res.rows)
    for ts in res.stats.per_search.values():
        assert ts.comments_kept <= ts.comments_fetched


def test_breaker_off_gives_hits_but_no_matches(fake, terms):
    res = run(fake, {"Craft": terms["Craft"]}, breaker_after=0)
    assert res.stats.summary()["verdicts"] == {"x :: Craft": "hits_but_no_matches"} and len(fake.fetched) == 21


def test_fetch_error_does_not_abort(terms):
    fake = FakeClient(fail_url="/b1/")
    res = run(fake, {"Obsidian": terms["Obsidian"]}, breaker_after=0)
    assert res.stats.fetch_errors == 1 and res.stats.failed_posts[0]["post_id"] == "t3_b1"
    assert res.stats.comment_fetches == 1 and res.stats.summary()["fetch_errors"] == 1


def test_per_sub(fake, terms):
    res = run(fake, {"Obsidian": terms["Obsidian"]}, subs=("alpha", "beta"), breaker_after=0, per_sub=True)
    assert [s for s, q in fake.searched] == ["alpha", "beta"]
    assert set(res.stats.per_search) == {"alpha :: Obsidian", "beta :: Obsidian"}
    assert res.stats.posts_deduped == 2 and len(fake.fetched) == 2
    assert all(len(r.matched_terms) == 1 for r in res.rows)


def test_thread_context_rows_are_tagged(fake, terms):
    res = run(fake, {"Obsidian": terms["Obsidian"]}, breaker_after=0, thread_context=True)
    kinds = {r.matched_in for r in res.rows}
    assert kinds == {"post", "comment", "thread_context"}
    assert res.stats.rows["thread_context"] == 1  # the 'nothing relevant here' comment under b1


def test_scope_prefixes_query(fake, terms):
    run(fake, {"Obsidian": terms["Obsidian"]}, breaker_after=0, scope="title")
    assert fake.searched[0][1] == 'title:"obsidian"'


def test_circuit_breaker_unit():
    ts2 = TermStats(); b2 = CircuitBreaker(after=2)
    assert b2.record_evaluated("k", ts2) is False
    assert b2.record_evaluated("k", ts2) is True and b2.is_tripped("k") and ts2.breaker_tripped_after_posts == 2
    ts3 = TermStats(posts_kept=1); b3 = CircuitBreaker(after=1)
    assert b3.record_evaluated("k", ts3) is False, "a term with rows never trips"
    assert CircuitBreaker(after=0).record_evaluated("k", TermStats()) is False, "0 disables"


def test_stats_dict_shape_and_conditional_key(fake, terms):
    d = run(fake, terms, breaker_after=5).stats.to_dict()
    assert list(d) == ["per_search", "discarded", "posts_seen", "posts_deduped", "comment_fetches", "comments_total",
                       "posts_skipped_by_breaker", "fetch_errors", "failed_posts", "rows", "summary"]
    craft, things = d["per_search"]["x :: Craft"], d["per_search"]["x :: Things"]
    assert "breaker_tripped_after_posts" in craft and "breaker_tripped_after_posts" not in things
    assert set(d["summary"]) >= {"candidates_evaluated", "discard_rate_strict", "verdicts", "elapsed_seconds"}


def test_facade_returns_dicts(fake, terms):
    rows, stats = run_batch(fake, ["x"], terms, 50, "relevance", "all", 0, breaker_after=5, progress_every=0, zero_retry_delay=0, reporter=NullReporter())
    assert isinstance(rows[0], dict) and list(rows[0]) == ["sub", "post_id", "post_title", "item_id", "item_type", "author", "body",
                                                             "url", "updated", "links", "images", "matched_terms", "matched_in", "snippet", "author_flags"]
    assert stats["summary"]["verdicts"]["x :: Tana"] == "confirmed_zero_presence"


def test_author_flags_on_batch_rows(fake, terms):
    from reddit_rss_miner import AuthorFlagger
    o = BatchOptions(limit=50, delay=0, progress_every=0, breaker_after=0)
    res = BatchRunner(fake, {"Obsidian": terms["Obsidian"]}, o, NullReporter(), sleep=NO_SLEEP, clock=lambda: 0.0,
                      flagger=AuthorFlagger.from_specs(["official:b"])).run(["x"])
    flags = {r.item_type: r.author_flags for r in res.rows}
    assert flags["comment"] == ["official"] and flags["post"] == []


def test_sinks(fake, terms, tmp_path):
    res = run(fake, terms, breaker_after=5)
    sink = JsonlFileSink(tmp_path / "out.jsonl"); sink.write_rows(res.rows); sink.write_stats(res.stats)
    assert sink.stats_path == tmp_path / "out.stats.json" and sink.stats_path.exists()
    assert len((tmp_path / "out.jsonl").read_text().splitlines()) == len(res.rows)
    mem = InMemorySink(); mem.write_rows(res.rows); mem.write_stats(res.stats)
    assert mem.rows == res.rows and mem.stats is res.stats
