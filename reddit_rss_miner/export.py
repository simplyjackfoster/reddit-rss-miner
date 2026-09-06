"""Typed Parquet export and ad-hoc SQL over miner output, via DuckDB (optional dependency).

Why declared types: DuckDB infers JSONL column types from the data, so a file whose rows never
carry links, flags or snippets comes out with generic JSON columns and queries written against one
file break on another. ``ROW_SCHEMA`` pins every column's type; every Parquet file this module writes
has the same schema regardless of content, and several JSONL files (batch runs, crawls) merge into
one table with a ``source`` column naming the file each row came from.

Install with ``pip install 'reddit-rss-miner[query]'`` or ``pip install duckdb``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

ROW_SCHEMA: dict[str, str] = {
    "sub": "VARCHAR",
    "post_id": "VARCHAR",
    "post_title": "VARCHAR",
    "item_id": "VARCHAR",
    "item_type": "VARCHAR",
    "author": "VARCHAR",
    "body": "VARCHAR",
    "url": "VARCHAR",
    "updated": "VARCHAR",          # ISO-8601 string in JSONL; exported as updated_at TIMESTAMPTZ as well
    "links": "VARCHAR[]",
    "images": "VARCHAR[]",
    "matched_terms": "VARCHAR[]",
    "matched_in": "VARCHAR",
    "snippet": "VARCHAR",
    "author_flags": "VARCHAR[]",
}

EXPORTED_COLUMNS: tuple[str, ...] = tuple(ROW_SCHEMA) + ("updated_at", "source")


class DuckDBMissing(RuntimeError):
    pass


def _duckdb():
    try:
        import duckdb
    except ImportError as e:  # pragma: no cover
        raise DuckDBMissing("DuckDB is not installed. Run: pip install duckdb   (or pip install 'reddit-rss-miner[query]')") from e
    return duckdb


def _sql_str(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _columns_literal() -> str:
    return "{" + ", ".join(f"{name}: {_sql_str(typ)}" for name, typ in ROW_SCHEMA.items()) + "}"


def rows_query(jsonl_paths: Sequence[str | Path]) -> str:
    """SQL producing the typed, unioned row table for the given JSONL files."""
    parts = []
    for p in jsonl_paths:
        path = Path(p)
        parts.append(
            f"SELECT {', '.join(ROW_SCHEMA)}, "
            f"try_cast(nullif(updated, '') AS TIMESTAMPTZ) AS updated_at, {_sql_str(path.name)} AS source "
            f"FROM read_json({_sql_str(str(path))}, format='newline_delimited', columns={_columns_literal()})"
        )
    return " UNION ALL ".join(parts)


def write_parquet(jsonl_paths: Sequence[str | Path], parquet_path: str | Path) -> int:
    """Convert one or more row JSONL files into a single typed Parquet file. Returns the row count."""
    duckdb = _duckdb()
    con = duckdb.connect()
    con.execute(f"COPY ({rows_query(jsonl_paths)}) TO {_sql_str(str(parquet_path))} (FORMAT PARQUET)")
    return con.execute(f"SELECT count(*) FROM read_parquet({_sql_str(str(parquet_path))})").fetchone()[0]


def connect(sources: Iterable[str | Path]):
    """DuckDB connection with a ``rows`` view over the given .parquet and/or .jsonl files."""
    duckdb = _duckdb()
    con = duckdb.connect()
    parquet = [str(p) for p in sources if str(p).endswith(".parquet")]
    jsonl = [p for p in sources if not str(p).endswith(".parquet")]
    selects = []
    if parquet:
        selects.append(f"SELECT {', '.join(EXPORTED_COLUMNS)} FROM read_parquet([{', '.join(_sql_str(p) for p in parquet)}])")
    if jsonl:
        selects.append(rows_query(jsonl))
    if not selects:
        raise ValueError("no sources given")
    con.execute(f"CREATE VIEW rows AS {' UNION ALL '.join(selects)}")
    return con


def query(sources: Iterable[str | Path], sql: str) -> tuple[list[str], list[tuple]]:
    """Run ``sql`` against the ``rows`` view. Returns (column names, rows)."""
    con = connect(sources)
    cur = con.execute(sql)
    return [d[0] for d in cur.description], cur.fetchall()
