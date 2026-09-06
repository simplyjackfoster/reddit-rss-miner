import json

import pytest

duckdb = pytest.importorskip("duckdb")

from reddit_rss_miner.export import EXPORTED_COLUMNS, ROW_SCHEMA, connect, query, rows_query, write_parquet


def row(**over):
    base = {"sub": "x", "post_id": "t3_a", "post_title": "T", "item_id": "t1_1", "item_type": "comment", "author": "bob",
            "body": "hello", "url": "https://www.reddit.com/r/x/comments/a/", "updated": "2026-01-02T03:04:05+00:00",
            "links": [], "images": [], "matched_terms": [], "matched_in": "dedicated_subreddit", "snippet": None, "author_flags": []}
    base.update(over)
    return base


def write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def test_parquet_schema_is_declared_not_inferred(tmp_path):
    """A crawl file (all lists empty, snippet null) and a batch file must produce identical column types."""
    crawl = write(tmp_path / "crawl.jsonl", [row(), row(item_id="t1_2", updated="")])
    batch = write(tmp_path / "batch.jsonl", [row(matched_terms=["Things"], matched_in="comment", snippet="s",
                                                 links=["https://e.org"], author_flags=["official"])])
    n1 = write_parquet([crawl], tmp_path / "c.parquet")
    n2 = write_parquet([batch], tmp_path / "b.parquet")
    assert (n1, n2) == (2, 1)
    con = duckdb.connect()
    types_c = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{tmp_path}/c.parquet')").fetchall()
    types_b = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{tmp_path}/b.parquet')").fetchall()
    assert [(n, t) for n, t, *_ in types_c] == [(n, t) for n, t, *_ in types_b]
    got = {n: t for n, t, *_ in types_c}
    assert got["links"] == "VARCHAR[]" and got["author_flags"] == "VARCHAR[]" and got["snippet"] == "VARCHAR"
    assert got["updated_at"] == "TIMESTAMP WITH TIME ZONE" and got["source"] == "VARCHAR"
    assert list(got) == list(EXPORTED_COLUMNS) and set(ROW_SCHEMA) < set(EXPORTED_COLUMNS)


def test_merge_adds_source_and_casts_updated(tmp_path):
    a = write(tmp_path / "a.jsonl", [row()])
    b = write(tmp_path / "b.jsonl", [row(item_id="t1_9", updated="")])
    write_parquet([a, b], tmp_path / "m.parquet")
    cols, rows = query([tmp_path / "m.parquet"], "SELECT source, item_id, updated_at IS NULL AS no_ts FROM rows ORDER BY source")
    assert cols == ["source", "item_id", "no_ts"] and rows == [("a.jsonl", "t1_1", False), ("b.jsonl", "t1_9", True)]


def test_connect_unions_parquet_and_jsonl(tmp_path):
    a = write(tmp_path / "a.jsonl", [row()])
    write_parquet([a], tmp_path / "a.parquet")
    b = write(tmp_path / "b.jsonl", [row(item_id="t1_2", author="Alice", author_flags=["official"])])
    con = connect([tmp_path / "a.parquet", b])
    assert con.execute("SELECT count(*) FROM rows").fetchone()[0] == 2
    assert con.execute("SELECT author FROM rows WHERE list_contains(author_flags, 'official')").fetchall() == [("Alice",)]


def test_rows_query_escapes_paths():
    assert "it''s.jsonl" in rows_query(["it's.jsonl"])
