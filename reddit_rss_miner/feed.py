"""Atom feed parsing and content-HTML utilities. No network, no Reddit URL knowledge."""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from typing import Iterator

ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}

_REDDIT_INTERNAL = re.compile(
    r"^https?://(?:www\.|old\.)?reddit\.com/(?:user/|u/|message/|r/[^/]+/?$|r/[^/]+/comments/)", re.I
)
_VIDEO_HOSTS = ("youtube.com", "youtu.be", "v.redd.it", "vimeo.com", "streamable.com", "redgifs.com")
_ABSOLUTE_URL = re.compile(r"https?://", re.I)


@dataclass
class Entry:
    """One Atom entry: a post (``t3_`` id) or a comment (``t1_`` id)."""
    id: str
    title: str
    author: str | None
    url: str | None
    updated: str
    body: str
    links: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)

    @property
    def is_post(self) -> bool:
        return self.id.startswith("t3_")

    @property
    def is_comment(self) -> bool:
        return self.id.startswith("t1_")

    def to_dict(self) -> dict:
        return asdict(self)


def strip_html(content: str) -> str:
    content = re.sub(r"<!--.*?-->", "", content or "", flags=re.S)
    content = re.sub(r"<br\s*/?>|</p>|</li>", "\n", content)
    content = re.sub(r"<[^>]+>", "", content)
    return html.unescape(content).strip()


def extract_media(content_html: str) -> dict[str, list[str]]:
    """Outbound links, images and videos from an entry's content HTML.

    Reddit wraps the body in boilerplate anchors ("submitted by /u/x", "[link]", "[comments]").
    Reddit-internal and relative anchors are dropped, so a link post yields its external target
    and a self post or comment yields only the URLs the author wrote.
    """
    hrefs = [html.unescape(h) for h in re.findall(r'<a\s[^>]*?href="([^"]+)"', content_html or "", re.I)]
    images = [html.unescape(u) for u in re.findall(r'<img\s[^>]*?src="([^"]+)"', content_html or "", re.I)]
    links: list[str] = []
    videos: list[str] = []
    seen = set(images)
    for href in hrefs:
        if href in seen or _REDDIT_INTERNAL.match(href) or not _ABSOLUTE_URL.match(href):
            continue
        seen.add(href)
        (videos if any(host in href for host in _VIDEO_HOSTS) else links).append(href)
    return {"links": links, "images": images, "videos": videos}


def parse_entries(xml_bytes: bytes) -> Iterator[Entry]:
    root = ET.fromstring(xml_bytes)
    for element in root.findall("a:entry", ATOM_NS):
        author = element.find("a:author/a:name", ATOM_NS)
        link = element.find("a:link", ATOM_NS)
        content = element.findtext("a:content", default="", namespaces=ATOM_NS)
        yield Entry(
            id=element.findtext("a:id", default="", namespaces=ATOM_NS),
            title=element.findtext("a:title", default="", namespaces=ATOM_NS),
            author=(author.text if author is not None else "").removeprefix("/u/") or None,
            url=link.get("href") if link is not None else None,
            updated=element.findtext("a:updated", default="", namespaces=ATOM_NS),
            body=strip_html(content),
            **extract_media(content),
        )
