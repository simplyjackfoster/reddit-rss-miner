"""Product term definitions and the per-item relevance matcher.

A *mention* is any alias hit on a word boundary. A mention inside an ``exclude`` match does not
count. If ``require`` is set, at least one context word must co-occur unless a ``strong_alias``
is present. See terms.example.json.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

REASON_OK = "ok"
REASON_NO_MENTION = "no_mention"
REASON_EXCLUDED = "excluded_pattern"
REASON_MISSING_CONTEXT = "missing_required_context"
DISCARD_REASONS = (REASON_NO_MENTION, REASON_EXCLUDED, REASON_MISSING_CONTEXT)

SNIPPET_RADIUS = 80


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    reason: str
    snippet: str | None = None

    def __iter__(self):  # keeps the legacy ``ok, reason, snippet = match_term(...)`` unpacking working
        yield self.matched
        yield self.reason
        yield self.snippet


def _word_bounded(words: Iterable[str], flags: int) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(w) for w in words) + r")(?!\w)", flags)


@dataclass(frozen=True)
class Term:
    name: str
    search: str
    alias_re: re.Pattern[str]
    strong_re: re.Pattern[str] | None
    require_re: re.Pattern[str] | None
    exclude_res: tuple[re.Pattern[str], ...]

    @classmethod
    def from_config(cls, name: str, cfg: Mapping | list) -> "Term":
        if isinstance(cfg, list):
            cfg = {"aliases": cfg}
        aliases = list(cfg.get("aliases") or [name])
        strong = list(cfg.get("strong_aliases", []))
        flags = 0 if cfg.get("case_sensitive") else re.IGNORECASE
        return cls(
            name=name,
            search=cfg.get("search") or f'"{name}"',
            alias_re=_word_bounded(aliases + strong, flags),
            strong_re=_word_bounded(strong, flags) if strong else None,
            require_re=_word_bounded(cfg["require"], re.I) if cfg.get("require") else None,
            exclude_res=tuple(re.compile(x, re.I) for x in cfg.get("exclude", [])),
        )

    def match(self, text: str) -> MatchResult:
        mentions = list(self.alias_re.finditer(text))
        if not mentions:
            return MatchResult(False, REASON_NO_MENTION)
        excluded = [m.span() for x in self.exclude_res for m in x.finditer(text)]
        good = [m for m in mentions if not any(a <= m.start() < b for a, b in excluded)]
        if not good:
            return MatchResult(False, REASON_EXCLUDED)
        if self.require_re and not self.require_re.search(text) and not (self.strong_re and self.strong_re.search(text)):
            return MatchResult(False, REASON_MISSING_CONTEXT)
        m = good[0]
        lo, hi = max(0, m.start() - SNIPPET_RADIUS), min(len(text), m.end() + SNIPPET_RADIUS)
        return MatchResult(True, REASON_OK, re.sub(r"\s+", " ", text[lo:hi]).strip())


def load_terms(path: str | Path) -> dict[str, Term]:
    raw = json.loads(Path(path).read_text())
    return {name: Term.from_config(name, cfg) for name, cfg in raw.items()}


def match_term(text: str, term: Term) -> MatchResult:
    """Function form kept for callers written against the single-file version."""
    return term.match(text)
