"""Explicit author flagging. RSS exposes no flair or moderator status, so nothing is inferred:
every flag comes from a user-supplied list of usernames."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

DEFAULT_LABEL = "flagged"


@dataclass
class AuthorFlagger:
    rules: dict[str, frozenset[str]] = field(default_factory=dict)   # label -> lowercased usernames

    @classmethod
    def from_specs(cls, specs: Iterable[str]) -> "AuthorFlagger":
        """Each spec is ``name1,name2`` (label 'flagged') or ``label:name1,name2``. Case-insensitive."""
        rules: dict[str, set[str]] = {}
        for spec in specs:
            label, _, names = spec.partition(":") if ":" in spec else (DEFAULT_LABEL, "", spec)
            rules.setdefault(label.strip(), set()).update(n.strip().lower().removeprefix("u/") for n in names.split(",") if n.strip())
        return cls({k: frozenset(v) for k, v in rules.items()})

    def flags(self, author: str | None) -> list[str]:
        if not author:
            return []
        a = author.lower()
        return [label for label, names in self.rules.items() if a in names]
