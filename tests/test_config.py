"""Tests for the plug inventory parsing.

`TAPO_PLUGS` is the one place a wrong value can silently point the sync at the
wrong sub-queue, so the parsing is worth pinning down.
"""

from __future__ import annotations

from tapo_scheduler.config import PlugTarget, parse_target


def test_plain_address_has_no_queue() -> None:
    assert parse_target("192.168.68.62") == PlugTarget(ip="192.168.68.62", queue=None)


def test_address_with_sub_queue() -> None:
    assert parse_target("192.168.68.62:3.1") == PlugTarget(ip="192.168.68.62", queue="3.1")


def test_bare_queue_number_is_accepted() -> None:
    """The operator labels some posts with a plain "3" instead of "3.1"."""
    assert parse_target("192.168.68.62:3") == PlugTarget(ip="192.168.68.62", queue="3")


def test_whitespace_is_tolerated() -> None:
    assert parse_target("  192.168.68.62 : 2.2  ") == PlugTarget(
        ip="192.168.68.62", queue="2.2"
    )


def test_hostname_with_a_queue_is_kept() -> None:
    assert parse_target("boiler.local:3.1") == PlugTarget(ip="boiler.local", queue="3.1")


def test_bare_hostname_has_no_queue() -> None:
    assert parse_target("plug.local") == PlugTarget(ip="plug.local", queue=None)


def test_trailing_colon_is_not_a_queue() -> None:
    assert parse_target("192.168.68.62:") == PlugTarget(ip="192.168.68.62:", queue=None)


def test_ipv6_literal_is_left_whole() -> None:
    """A colon in the head means IPv6, so the tail is not read as a queue.

    `fe80::1` and `fe80::1:3.1` are both valid addresses, and the last colon
    cannot tell a queue separator apart from an address byte.
    """
    assert parse_target("fe80::1") == PlugTarget(ip="fe80::1", queue=None)
    assert parse_target("fe80::1:3.1") == PlugTarget(ip="fe80::1:3.1", queue=None)


def test_str_round_trips() -> None:
    assert str(parse_target("192.168.68.62:3.1")) == "192.168.68.62:3.1"
    assert str(parse_target("192.168.68.62")) == "192.168.68.62"
