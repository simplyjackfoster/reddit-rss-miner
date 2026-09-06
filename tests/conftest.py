import json
import tempfile
from pathlib import Path

import pytest

from reddit_rss_miner import Entry, load_terms

TERMS_CFG = {
    "Things": {"aliases": ["things"], "strong_aliases": ["things 3", "things app", "culturedcode.com"], "search": '"things 3"',
               "require": ["app", "task", "tasks", "todo", "mac", "ios", "todoist", "ticktick", "$"],
               "exclude": ["things to\\b", "things (?:i|you|we|they|like|that|are)\\b",
                           "\\b(?:all|many|some|big|first) things", "^things$", "things\\s*!", "(?:^|[.!?]\\s+)things\\s*\\."]},
    "Obsidian": {"aliases": ["obsidian"], "search": '"obsidian"'},
    "Tana": {"aliases": ["tana"], "search": '"tana"'},
    "Craft": {"aliases": ["craft"], "search": '"craft"'},
}


def post(i, title, body=""):
    return Entry(id=f"t3_{i}", title=title, author="a", url=f"https://www.reddit.com/r/x/comments/{i}/x/", updated="", body=body)


def comment(i, body):
    return Entry(id=f"t1_{i}", title="", author="b", url="u", updated="", body=body)


class FakeClient:
    """Scenario: Things blanks on its first search then returns 3 posts; Tana has none; Craft returns
    20 generic posts plus one shared with Obsidian; Obsidian returns 2. Post k2 is a megathread whose
    comments mention Things; post 'shared' is hit by Craft, Obsidian and Things."""

    def __init__(self, fail_url=None):
        self.calls, self.fetched, self.searched, self.fail_url = {}, [], [], fail_url

    def search(self, sub, q, limit=25, sort="relevance", t="all"):
        self.searched.append((sub, q))
        n = self.calls.get(q, 0)
        self.calls[q] = n + 1
        if "things" in q:
            return iter([] if n == 0 else [post("k1", "Things vs Todoist app"), post("k2", "daily thread"),
                                          post("shared", "Obsidian and the Things app")])
        if "tana" in q:
            return iter([])
        if "craft" in q:
            return iter([post(f"p{i}", f"generic post {i}") for i in range(20)] + [post("shared", "Obsidian and the Things app")])
        if "obsidian" in q:
            return iter([post("b1", "Obsidian vault review"), post("shared", "Obsidian and the Things app")])
        return iter([])

    def comments(self, url):
        self.fetched.append(url)
        if self.fail_url and self.fail_url in url:
            import requests
            raise requests.exceptions.ReadTimeout("simulated timeout")
        if "/k2/" in url:
            return None, [comment(1, "I use the Things app for tasks"), comment(2, "things to avoid!")]
        if "/shared/" in url:
            return None, [comment(3, "obsidian is great")]
        return None, [comment(9, "nothing relevant here")]


@pytest.fixture
def terms(tmp_path):
    p = tmp_path / "terms.json"
    p.write_text(json.dumps(TERMS_CFG))
    return load_terms(p)


@pytest.fixture
def fake():
    return FakeClient()


NO_SLEEP = lambda s: None
