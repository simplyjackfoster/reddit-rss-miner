import json

import pytest

from reddit_rss_miner.cli import build_parser, main

EXPECTED_FLAGS = {"--limit", "--sort", "--time", "--post", "--delay", "--out", "--subs", "--terms", "--rows", "--scope",
                  "--thread-context", "--breaker", "--progress", "--per-sub", "-h", "--help",
                  "--full-subreddit", "--listing-sort", "--max-posts", "--resume", "--flag-authors",
                  "--parquet", "--convert", "--query", "--from"}


def test_flag_set_unchanged():
    flags = {opt for a in build_parser()._actions for opt in a.option_strings}
    assert flags == EXPECTED_FLAGS


def test_defaults():
    a = build_parser().parse_args([])
    assert (a.limit, a.sort, a.time, a.delay, a.rows, a.scope, a.breaker, a.progress) == (10, "relevance", "all", 1.0, "rows.jsonl", "all", 12, 10)
    assert not a.thread_context and not a.per_sub


def test_missing_credentials_exit_code(monkeypatch):
    from reddit_rss_miner.config import MissingCredentials

    def boom():
        raise MissingCredentials("none")
    monkeypatch.setattr("reddit_rss_miner.cli.load_credentials", boom)
    assert main(["--post", "r/x/comments/1"]) == 2


def test_batch_command_writes_files(monkeypatch, tmp_path, terms):
    from conftest import FakeClient
    monkeypatch.setattr("reddit_rss_miner.cli.build_client", lambda delay=1.0: FakeClient())
    monkeypatch.setattr("reddit_rss_miner.cli.load_terms", lambda p: terms)
    monkeypatch.setattr("reddit_rss_miner.batch.time.sleep", lambda s: None)
    rows = tmp_path / "r.jsonl"
    assert main(["--subs", "x", "--terms", "ignored", "--rows", str(rows), "--breaker", "5", "--progress", "0", "--delay", "0"]) == 0
    stats = json.loads((tmp_path / "r.stats.json").read_text())
    assert stats["summary"]["verdicts"]["x :: Craft"] == "breaker_tripped"
    assert rows.read_text().count("\n") == stats["summary"]["rows_kept_strict"]


def test_crawl_command_writes_rows_and_summary(monkeypatch, tmp_path):
    from test_crawl import ListingFake
    monkeypatch.setattr("reddit_rss_miner.cli.build_client", lambda delay=1.0: ListingFake(7, page=5))
    rows = tmp_path / "c.jsonl"
    assert main(["--full-subreddit", "x", "--rows", str(rows), "--progress", "0", "--flag-authors", "official:b"]) == 0
    summary = json.loads((tmp_path / "c.crawl.json").read_text())
    assert summary["posts_fetched"] == 7 and summary["exhausted"] and not summary["ceiling_suspected"]
    lines = [json.loads(l) for l in rows.read_text().splitlines()]
    assert len(lines) == 21 and all(l["matched_in"] == "dedicated_subreddit" for l in lines)
    assert sum(1 for l in lines if l["author_flags"] == ["official"]) == 14


def test_convert_and_query_need_no_credentials(monkeypatch, tmp_path, capsys):
    pytest.importorskip("duckdb")
    from reddit_rss_miner.config import MissingCredentials
    monkeypatch.setattr("reddit_rss_miner.cli.load_credentials", lambda: (_ for _ in ()).throw(MissingCredentials("none")))
    src = tmp_path / "r.jsonl"
    src.write_text(json.dumps({"sub": "x", "post_id": "t3_a", "post_title": "T", "item_id": "t3_a", "item_type": "post", "author": "a",
                               "body": "b", "url": "u", "updated": "", "links": [], "images": [], "matched_terms": [],
                               "matched_in": "dedicated_subreddit", "snippet": None, "author_flags": []}) + "\n")
    pq = tmp_path / "r.parquet"
    assert main(["--convert", str(src), "--parquet", str(pq)]) == 0 and pq.exists()
    assert main(["--query", "SELECT count(*) AS n FROM rows", "--from", str(pq)]) == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == "1"
    assert main(["--convert", str(src)]) == 2
    assert main(["--query", "SELECT 1"]) == 2
    assert main(["--query", "SELECT nope FROM rows", "--from", str(pq)]) == 1
    assert "query failed" in capsys.readouterr().err
