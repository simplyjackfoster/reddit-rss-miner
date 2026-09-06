from reddit_rss_miner.terms import Term, match_term

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


def test_collision_filter_precision_and_recall(terms):
    t = terms["Things"]
    for text, expected in COLLISION_CASES.items():
        assert t.match(text).matched is expected, text


def test_reasons(terms):
    t = terms["Things"]
    assert t.match("nothing here").reason == "no_mention"
    assert t.match("Things to do").reason == "excluded_pattern"
    assert t.match("I like Things a lot").reason == "missing_required_context"
    ok = t.match("Things app is nice")
    assert ok.reason == "ok" and "Things app" in ok.snippet


def test_legacy_unpacking_and_defaults(terms):
    ok, reason, snippet = match_term("Obsidian rocks", terms["Obsidian"])
    assert ok and reason == "ok" and snippet == "Obsidian rocks"
    plain = Term.from_config("Foo", ["foo", "foobar"])
    assert plain.search == '"Foo"' and plain.match("FOO!").matched and not plain.match("food").matched
    cs = Term.from_config("Bear", {"aliases": ["Bear"], "case_sensitive": True})
    assert cs.match("Bear notes").matched and not cs.match("bear notes").matched
