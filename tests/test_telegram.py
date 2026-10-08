"""Tests for the Telegram preview scraper.

The fixture is a verbatim slice of a real ``https://t.me/s/<channel>`` page, so
these tests fail if the extraction logic regresses. They cannot detect Telegram
changing its markup -- only a live fetch can do that, which is what
``test_live.py`` is for.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from tapo_scheduler.telegram import Post, parse_preview

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "preview-1790.html"
PHOTO_FIXTURE = FIXTURES / "preview-1751.html"


def test_extracts_id_timestamp_and_text() -> None:
    posts = parse_preview(FIXTURE.read_text(encoding="utf-8"), "pat_cherkasyoblenergo")
    assert len(posts) == 1

    post = posts[0]
    assert post.id == 1790
    assert post.channel == "pat_cherkasyoblenergo"
    assert post.published == datetime(2026, 10, 6, 7, 19, 56, tzinfo=timezone.utc)
    assert post.text.startswith("Відповідно до команди НЕК")
    assert "ГОП" in post.text
    assert post.has_photo is False
    assert post.url == "https://t.me/pat_cherkasyoblenergo/1790"


def test_photo_is_an_anchor_not_a_div() -> None:
    """Photos ship as <a class="tgme_widget_message_photo_wrap">.

    Checking for that class under the div branch made `has_photo` silently
    always-False, which is how a photo-only post would slip through as "the
    operator posted nothing today".
    """
    posts = parse_preview(PHOTO_FIXTURE.read_text(encoding="utf-8"), "pat_cherkasyoblenergo")
    assert len(posts) == 1
    assert posts[0].id == 1751
    assert posts[0].has_photo is True
    assert posts[0].text != ""


def test_markup_without_posts_yields_nothing() -> None:
    assert parse_preview("<html><body><p>no posts here</p></body></html>", "x") == []


def test_post_without_an_id_is_skipped() -> None:
    """Layout stubs carry the message classes but no data-post."""
    page = '<div class="tgme_widget_message"><time datetime="2026-10-08T00:00:00+00:00"></time></div>'
    assert parse_preview(page, "x") == []


def test_text_is_captured_without_the_footer() -> None:
    """The timestamp lives in the footer, outside the text div."""
    posts = parse_preview(FIXTURE.read_text(encoding="utf-8"), "pat_cherkasyoblenergo")
    assert "views" not in posts[0].text
    assert "<" not in posts[0].text
