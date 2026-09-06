from reddit_rss_miner.client import RedditRSSClient, normalize_post_path

ENTRY = '<entry><id>{id}</id><title>t</title><link href="https://www.reddit.com/r/x/comments/{id}/s/"/><updated>u</updated><content type="html">b</content></entry>'


def feed(*ids):
    return ('<feed xmlns="http://www.w3.org/2005/Atom">' + "".join(ENTRY.format(id=i) for i in ids) + "</feed>").encode()


class FakeTransport:
    def __init__(self, pages):
        self.pages, self.calls = list(pages), []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        return self.pages.pop(0)


def test_normalize_post_path():
    assert normalize_post_path("https://www.reddit.com/r/x/comments/abc/some_slug/") == "/r/x/comments/abc/"
    assert normalize_post_path("r/x/comments/abc") == "/r/x/comments/abc/"
    assert normalize_post_path("https://old.reddit.com/r/x/comments/abc/") == "/r/x/comments/abc/"


def test_search_paginates_with_after_and_stops_at_limit():
    tr = FakeTransport([feed("t3_1", "t3_2"), feed("t3_3", "t3_4")])
    got = [e.id for e in RedditRSSClient(tr).search("a+b", "q", limit=3)]
    assert got == ["t3_1", "t3_2", "t3_3"]
    assert tr.calls[0][0] == "/r/a+b/search.rss"
    assert tr.calls[0][1]["after"] is None and tr.calls[0][1]["limit"] == 3 and tr.calls[0][1]["restrict_sr"] == 1
    assert tr.calls[1][1]["after"] == "t3_2" and tr.calls[1][1]["limit"] == 1


def test_search_stops_on_empty_page():
    tr = FakeTransport([feed("t3_1"), feed()])
    assert [e.id for e in RedditRSSClient(tr).search("x", "q", limit=10)] == ["t3_1"]
    assert len(tr.calls) == 2


def test_comments_splits_post_and_comments():
    tr = FakeTransport([feed("t3_p", "t1_a", "t1_b")])
    post, comments = RedditRSSClient(tr).comments("https://www.reddit.com/r/x/comments/p/slug/")
    assert post.id == "t3_p" and [c.id for c in comments] == ["t1_a", "t1_b"]
    assert tr.calls[0][0] == "/r/x/comments/p/.rss" and tr.calls[0][1]["limit"] == 500
