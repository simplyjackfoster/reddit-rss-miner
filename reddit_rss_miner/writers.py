"""Output sinks. The batch runner never touches the filesystem directly."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, Protocol

from .batch import BatchStats, Row


class RowSink(Protocol):
    def write_rows(self, rows: Iterable[Row]) -> None: ...
    def write_stats(self, stats: BatchStats) -> None: ...


class JsonlFileSink:
    """rows -> <path>; stats -> <path minus .jsonl>.stats.json"""

    def __init__(self, rows_path: str | Path) -> None:
        self.rows_path = Path(rows_path)
        self.stats_path = Path(re.sub(r"\.jsonl?$", "", str(rows_path)) + ".stats.json")

    def write_rows(self, rows: Iterable[Row]) -> None:
        with self.rows_path.open("w") as f:
            for row in rows:
                f.write(json.dumps(row.to_dict(), ensure_ascii=False) + "\n")

    def write_stats(self, stats: BatchStats) -> None:
        self.stats_path.write_text(json.dumps(stats.to_dict(), indent=2))


class InMemorySink:
    def __init__(self) -> None:
        self.rows: list[Row] = []
        self.stats: BatchStats | None = None

    def write_rows(self, rows: Iterable[Row]) -> None:
        self.rows = list(rows)

    def write_stats(self, stats: BatchStats) -> None:
        self.stats = stats


def write_simple_results(path: str | Path, results: list[dict]) -> None:
    Path(path).write_text(json.dumps(results, indent=2))
