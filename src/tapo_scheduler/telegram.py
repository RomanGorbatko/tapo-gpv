"""Read a public Telegram channel's web preview.

Telegram serves the last ~20 posts of any *public* channel as plain HTML at
``https://t.me/s/<channel>`` -- no API key, no user account, no MTProto session.
That is all the outage feed needs, because the operator posts the schedule as
text rather than only as an infographic.

Deliberately synchronous and dependency-free: the tapo client is async, but a
single GET per refresh does not justify threading it through the event loop.
Call it from async code with ``asyncio.to_thread``.

Caveats worth knowing:

* Only public channels have a preview. A private one returns a page with no
  posts at all, which this module reports as an empty list rather than an error.
* Posts that are only an image come back with ``text == ""``. The operator
  posts those too, so callers must tolerate empty text.
* Pagination walks backwards with ``?before=<post_id>``.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser

PREVIEW_URL = "https://t.me/s/{channel}"
DEFAULT_CHANNEL = "pat_cherkasyoblenergo"

# Telegram serves a bare urllib request fine, but a browser-shaped UA is cheap
# insurance against it deciding otherwise.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class TelegramError(RuntimeError):
    """The preview could not be fetched or did not look like a channel page."""


@dataclass(frozen=True)
class Post:
    id: int
    channel: str
    published: datetime
    text: str
    has_photo: bool

    @property
    def url(self) -> str:
        return f"https://t.me/{self.channel}/{self.id}"

    def __str__(self) -> str:
        kind = "photo" if self.has_photo else "text"
        return f"{self.url}  {self.published:%Y-%m-%d %H:%M}Z  {kind}  {len(self.text)} chars"


def _parse_timestamp(value: str) -> datetime:
    """Parse the ``<time datetime=...>`` attribute, which is ISO-8601 with offset."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class _PreviewParser(HTMLParser):
    """Pull ``(id, published, text, has_photo)`` out of one preview page.

    The markup nests text inside the message bubble, so capture is driven by
    div depth: start collecting when the ``tgme_widget_message_text`` div opens
    and stop when the div at that same depth closes.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.posts: list[dict] = []
        self._post: dict | None = None
        self._div_depth = 0
        self._capture_at: int | None = None
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attribute = dict(attrs)
        classes = (attribute.get("class") or "").split()

        # Photos are an <a>, not a <div>, so this cannot live under the div
        # branch below. Grouped albums produce several of these per post.
        if "tgme_widget_message_photo_wrap" in classes and self._post is not None:
            self._post["has_photo"] = True

        if tag == "div":
            self._div_depth += 1
            if "tgme_widget_message" in classes:
                self._post = {
                    "id": None,
                    "published": None,
                    "text": "",
                    "has_photo": False,
                }
                self.posts.append(self._post)
                post_ref = (attribute.get("data-post") or "").split("?")[0]
                if "/" in post_ref:
                    tail = post_ref.rsplit("/", 1)[-1]
                    if tail.isdigit():
                        self._post["id"] = int(tail)
            if "tgme_widget_message_text" in classes:
                self._capture_at = self._div_depth
                self._chunks = []

        elif tag == "br":
            if self._capture_at is not None:
                self._chunks.append("\n")

        elif tag == "time" and self._post is not None:
            stamp = attribute.get("datetime")
            if stamp:
                self._post["published"] = stamp

    def handle_endtag(self, tag: str) -> None:
        if tag != "div":
            return
        if self._capture_at is not None and self._div_depth == self._capture_at:
            if self._post is not None:
                self._post["text"] = "".join(self._chunks).strip()
            self._capture_at = None
            self._chunks = []
        self._div_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._capture_at is not None:
            self._chunks.append(data)


def parse_preview(page: str, channel: str) -> list[Post]:
    """Extract every post on one preview page, oldest first (page order)."""
    parser = _PreviewParser()
    parser.feed(page)

    posts: list[Post] = []
    for raw in parser.posts:
        # A message without an id or a timestamp is a layout stub, not a post.
        if raw["id"] is None or raw["published"] is None:
            continue
        posts.append(
            Post(
                id=raw["id"],
                channel=channel,
                published=_parse_timestamp(raw["published"]),
                text=raw["text"],
                has_photo=raw["has_photo"],
            )
        )
    return posts


def fetch_page(channel: str, before: int | None = None, timeout: float = 20.0) -> str:
    """GET one preview page. ``before`` walks towards older posts."""
    url = PREVIEW_URL.format(channel=channel)
    if before is not None:
        url += f"?before={before}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "uk,en;q=0.8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise TelegramError(f"GET {url} -> HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise TelegramError(f"GET {url} -> {exc.reason}") from exc


def fetch_posts(
    channel: str = DEFAULT_CHANNEL, limit: int = 40, timeout: float = 20.0
) -> list[Post]:
    """Fetch up to ``limit`` posts, oldest first.

    Pages hold about 20 posts, so a larger ``limit`` means several requests.
    Stops early once Telegram runs out of history.
    """
    collected: list[Post] = []
    seen: set[int] = set()
    before: int | None = None

    while len(collected) < limit:
        batch = [
            post
            for post in parse_preview(fetch_page(channel, before, timeout), channel)
            if post.id not in seen
        ]
        if not batch:
            break
        seen.update(post.id for post in batch)
        collected = batch + collected
        before = min(post.id for post in batch)

    return collected[-limit:]
