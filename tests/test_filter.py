"""Offline tests: no network. Run with `python3 -m pytest` or `python3 tests/test_filter.py`."""
import json, os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import reddit_rss_miner as m

m.time.sleep = lambda s: None  # never wait in tests

TERMS_CFG = {
    "Things": {"aliases": ["things"], "strong_aliases": ["things 3", "things app", "culturedcode.com"], "search": '"things 3"',
               "require": ["app", "task", "tasks", "todo", "mac", "ios", "todoist", "ticktick", "$"],
               "exclude": ["things to\\b", "things (?:i|you|we|they|like|that|are)\\b",
                           "\\b(?:all|many|some|big|first) things", "^things$", "things\\s*!", "(?:^|[.!?]\\s+)things\\s*\\."]},
    "Obsidian": {"aliases": ["obsidian"], "search": '"obsidian"'},
    "Tana": {"aliases": ["tana"], "search": '"tana"'},
    "Craft": {"aliases": ["craft"], "search": '"craft"'},
}


def load(cfg=TERMS_CFG):
    tf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(cfg, tf); tf.close()
    try:
        return m.load_terms(tf.name)
    finally:
        os.unlink(tf.name)


COLLISION_CASES = {
    "Things, Todoist and TickTick to see which fits": True,
    "I was looking into Things at $49.99 for the Mac version": True,
    "https://culturedcode.com/things/": True,
    "I use Things but only for personal tasks": True,
    "I switched my task manager to Things.": True,
    "Has anyone tried the Things app for recurring tasks?": True,
    "Things to consider before switching apps": False,
    "First things first, back up your Mac": False,
    "All things considered, the app is fine.": False,
    "Lots of things!": False,
    "There are things I like about the app": False,
    "big things coming to the app": False,
    "things": False,
    "It handles many things well": False,
}


def test_collision_filter_precision_and_recall():
    t = load()["Things"]
    for text, expected in COLLISION_CASES.items():
        assert m.match_term(text, t)[0] is expected, text


def test_author_prefix_strip():
    assert "/u/user_x".removeprefix("/u/") == "user_x"


def P(i, title, body=""):
    return {"id": f"t3_{i}", "title": title, "body": body, "author": "a",
            "url": f"https://www.reddit.com/r/x/comments/{i}/x/", "updated": ""}


def C(i, body):
    return {"id": f"t1_{i}", "body": body, "author": "b", "url": "u", "updated": ""}


class Fake:
    def __init__(self, fail_url=None):
        self.calls, self.fetched, self.fail_url = {}, [], fail_url

    def search(self, sub, q, limit, sort, t):
        self.searched = getattr(self, "searched", []) + [(sub, q)]
        n = self.calls.get(q, 0); self.calls[q] = n + 1
        if "things" in q:  # transient empty first response
            return iter([] if n == 0 else [P("k1", "Things vs Todoist app"), P("k2", "daily thread"), P("shared", "Obsidian and the Things app")])
        if "tana" in q:
            return iter([])
        if "craft" in q:
            return iter([P(f"p{i}", f"generic post {i}") for i in range(20)] + [P("shared", "Obsidian and the Things app")])
        if "obsidian" in q:
            return iter([P("b1", "Obsidian vault review"), P("shared", "Obsidian and the Things app")])
        return iter([])

    def comments(self, url):
        self.fetched.append(url)
        if self.fail_url and self.fail_url in url:
            raise m.requests.exceptions.ReadTimeout("simulated timeout")
        if "/k2/" in url:
            return None, [C(1, "I use the Things app for tasks"), C(2, "things to avoid!")]
        if "/shared/" in url:
            return None, [C(3, "obsidian is great")]
        return None, [C(9, "nothing relevant here")]


def test_batch_retry_breaker_verdicts():
    fk = Fake()
    rows, st = m.run_batch(fk, ["x"], load(), 50, "relevance", "all", 0, breaker_after=5, progress_every=0)
    ps = {k.split(" :: ")[1]: v for k, v in st["per_search"].items()}
    assert all(k.startswith("x :: ") for k in st["per_search"]), "pooled mode keys are '<subs> :: <term>'"
    assert ps["Things"]["search_attempts"] == 2 and ps["Things"]["zero_hits_recovered_on_retry"] and ps["Things"]["search_hits"] == 3
    assert ps["Tana"]["verdict"] == "confirmed_zero_presence" and ps["Tana"]["search_attempts"] == 2
    assert ps["Craft"]["verdict"] == "breaker_tripped" and ps["Craft"]["breaker_tripped_after_posts"] == 5
    assert st["posts_skipped_by_breaker"] >= 10
    assert any("/shared/" in u for u in fk.fetched), "post shared with an active term must still be fetched"
    assert ps["Things"]["verdict"] == "ok" and ps["Things"]["comments_kept"] == 1
    assert ps["Craft"]["post_level_precision"] == 0.0 and ps["Obsidian"]["post_level_precision"] == 1.0
    assert not any(r["post_id"] == "t3_k2" and r["item_type"] == "post" for r in rows), "megathread must not be a post row"


def test_breaker_off_gives_hits_but_no_matches():
    terms = load(); fk = Fake()
    _, st = m.run_batch(fk, ["x"], {"Craft": terms["Craft"]}, 50, "relevance", "all", 0, breaker_after=0, progress_every=0)
    assert st["summary"]["verdicts"] == {"x :: Craft": "hits_but_no_matches"} and len(fk.fetched) == 21


def test_fetch_error_does_not_abort_run():
    terms = load(); fk = Fake(fail_url="/b1/")
    rows, st = m.run_batch(fk, ["x"], {"Obsidian": terms["Obsidian"]}, 50, "relevance", "all", 0, breaker_after=0, progress_every=0)
    assert st["fetch_errors"] == 1 and st["failed_posts"][0]["post_id"] == "t3_b1"
    assert st["comment_fetches"] == 1 and st["summary"]["fetch_errors"] == 1


def test_per_sub_searches_each_subreddit_separately():
    terms = load(); fk = Fake()
    rows, st = m.run_batch(fk, ["alpha", "beta"], {"Obsidian": terms["Obsidian"]}, 50, "relevance", "all", 0,
                           breaker_after=0, progress_every=0, per_sub=True)
    subs_searched = [s for s, q in fk.searched if "obsidian" in q]
    assert subs_searched == ["alpha", "beta"], subs_searched
    assert set(st["per_search"]) == {"alpha :: Obsidian", "beta :: Obsidian"}
    assert st["posts_deduped"] == 2, "same posts returned for both subs must be deduped, fetched once"
    assert len([u for u in fk.fetched]) == 2
    assert all(len(r["matched_terms"]) == 1 for r in rows), "a term matched via two subs must appear once per row"


def test_extract_media_drops_reddit_boilerplate_keeps_outbound():
    content = ('<div class="md"><p>Compare <a href="https://culturedcode.com/things/">Things</a> and '
               '<a href="https://www.youtube.com/watch?v=abc">this video</a> <img src="https://i.redd.it/x.png"></p></div>'
               ' submitted by <a href="https://www.reddit.com/user/someone"> /u/someone </a> '
               '<span><a href="https://example.org/review">[link]</a></span> '
               '<span><a href="https://www.reddit.com/r/productivity/comments/abc123/x/">[comments]</a></span>')
    got = m.extract_media(content)
    assert got["links"] == ["https://culturedcode.com/things/", "https://example.org/review"], got
    assert got["videos"] == ["https://www.youtube.com/watch?v=abc"]
    assert got["images"] == ["https://i.redd.it/x.png"]
    assert m.extract_media("") == {"links": [], "images": [], "videos": []}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("PASS", name)
