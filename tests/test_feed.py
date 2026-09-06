from reddit_rss_miner.feed import extract_media, parse_entries, strip_html

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <link rel="self" href="https://www.reddit.com/r/x/search.rss?q=a"/>
  <entry>
    <author><name>/u/alice</name></author>
    <content type="html">&lt;!-- SC_OFF --&gt;&lt;div class="md"&gt;&lt;p&gt;Hello &amp;amp; welcome&lt;br/&gt;line 2&lt;/p&gt;&lt;/div&gt;&lt;!-- SC_ON --&gt; submitted by &lt;a href="https://www.reddit.com/user/alice"&gt;/u/alice&lt;/a&gt; &lt;a href="https://example.org/x"&gt;[link]&lt;/a&gt;</content>
    <id>t3_abc</id>
    <link href="https://www.reddit.com/r/x/comments/abc/hello/"/>
    <updated>2026-01-01T00:00:00+00:00</updated>
    <title>Hello post</title>
  </entry>
  <entry>
    <content type="html">&lt;div class="md"&gt;&lt;p&gt;a comment&lt;/p&gt;&lt;/div&gt;</content>
    <id>t1_def</id>
    <link href="https://www.reddit.com/r/x/comments/abc/comment/def/"/>
    <updated>2026-01-02T00:00:00+00:00</updated>
    <title>c</title>
  </entry>
</feed>"""


def test_parse_entries_post_and_comment():
    post, com = list(parse_entries(FEED))
    assert post.is_post and post.id == "t3_abc" and post.author == "alice" and post.title == "Hello post"
    assert post.body == "Hello & welcome\nline 2\n submitted by /u/alice [link]"
    assert post.links == ["https://example.org/x"] and post.images == [] and post.videos == []
    assert com.is_comment and com.author is None and com.body == "a comment"


def test_strip_html_newlines_and_entities():
    assert strip_html("<p>a</p><p>b&lt;c</p><ul><li>d</li></ul>") == "a\nb<c\nd"
    assert strip_html("") == ""


def test_extract_media_drops_reddit_boilerplate_keeps_outbound():
    content = ('<div class="md"><p>Compare <a href="https://culturedcode.com/things/">Things</a> and '
               '<a href="https://www.youtube.com/watch?v=abc">this video</a> <img src="https://i.redd.it/x.png"></p></div>'
               ' submitted by <a href="https://www.reddit.com/user/someone"> /u/someone </a> '
               '<span><a href="https://example.org/review">[link]</a></span> '
               '<span><a href="https://www.reddit.com/r/productivity/comments/abc123/x/">[comments]</a></span>'
               '<a href="/r/Productivity">rel</a> <a href="https://www.reddit.com/message/compose/?to=x">msg</a>')
    got = extract_media(content)
    assert got["links"] == ["https://culturedcode.com/things/", "https://example.org/review"]
    assert got["videos"] == ["https://www.youtube.com/watch?v=abc"]
    assert got["images"] == ["https://i.redd.it/x.png"]
    assert extract_media("") == {"links": [], "images": [], "videos": []}
