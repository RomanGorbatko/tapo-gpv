"""The clock is pinned to Kyiv, not to the machine running it.

A ГПВ schedule is published under a Kyiv date and the plugs fire on Kyiv wall
time, so "today" here has to mean today in Kyiv wherever the process sits. A
container defaults to UTC, and the gap is not academic: at 22:30 UTC it is
already 01:30 of the next day in Kyiv, so a host-local date would fetch
yesterday's schedule and the boiler would run through a real outage.

The laptop this was written on is itself on Kyiv time, which is exactly why
these tests assert on fixed instants rather than on "now".
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from tapo_scheduler import clock


def at(iso: str) -> datetime:
    """A UTC instant, written the way the tests care about it."""
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


def kyiv_offset(iso: str) -> timedelta:
    return at(iso).astimezone(clock.KYIV).utcoffset()


# --- what the clock answers ------------------------------------------------


def test_now_is_timezone_aware_and_on_kyiv() -> None:
    """The guard that matters.

    A naive `datetime.now()` would still produce plausible dates, so nothing
    else in this file reliably catches a regression. This does: the identity
    check fails the moment the Kyiv zone stops being attached.
    """
    assert clock.now().tzinfo is clock.KYIV


def test_today_is_the_date_the_clock_reports() -> None:
    assert clock.today() == clock.now().date()


# --- the day boundary ------------------------------------------------------


def test_a_late_utc_evening_is_already_tomorrow_in_kyiv() -> None:
    """22:30 UTC is 01:30 the next day in Kyiv -- the case a UTC host gets wrong."""
    assert clock.kyiv_date(at("2026-10-08T22:30")) == date(2026, 10, 9)


def test_an_earlier_utc_evening_is_still_today() -> None:
    """The other side of that boundary, so the test above cannot pass by luck."""
    assert clock.kyiv_date(at("2026-10-08T20:59")) == date(2026, 10, 8)


def test_the_boundary_follows_the_season() -> None:
    """23:30 UTC is tomorrow in summer but still today in winter."""
    assert clock.kyiv_date(at("2026-07-15T23:30")) == date(2026, 7, 16)
    assert clock.kyiv_date(at("2026-01-15T23:30")) == date(2026, 1, 16)
    assert clock.kyiv_date(at("2026-01-15T21:30")) == date(2026, 1, 15)


# --- summer time -----------------------------------------------------------
#
# An hour of drift is not cosmetic: the rules carry clock times, so an offset
# the schedule was not written against moves every outage by that hour.


def test_the_offset_follows_summer_time() -> None:
    assert kyiv_offset("2026-01-15T12:00") == timedelta(hours=2)  # EET
    assert kyiv_offset("2026-07-15T12:00") == timedelta(hours=3)  # EEST


def test_the_clocks_change_on_the_last_sundays() -> None:
    """2026: forward on 29 March, back on 25 October, at 03:00 local."""
    assert kyiv_offset("2026-03-29T00:30") == timedelta(hours=2)
    assert kyiv_offset("2026-03-29T01:30") == timedelta(hours=3)
    assert kyiv_offset("2026-10-25T00:30") == timedelta(hours=3)
    assert kyiv_offset("2026-10-25T01:30") == timedelta(hours=2)


# --- the host --------------------------------------------------------------


@pytest.fixture
def host_timezone(monkeypatch):
    """Move the host's timezone, and put the process back afterwards.

    Restoring needs its own `tzset()`: undoing the environment variable alone
    leaves the C library still holding the zone it last read.
    """
    original = os.environ.get("TZ")
    yield lambda name: (monkeypatch.setenv("TZ", name), time.tzset())
    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs time.tzset")
def test_the_host_timezone_cannot_move_this_clock(host_timezone) -> None:
    """TZ moves the host. It must not move this.

    `Pacific/Kiritimati` is UTC+14, the furthest a date can get from Kyiv in
    one hop, so if the host were consulted at all the gap would show here.
    """
    host_timezone("Pacific/Kiritimati")

    assert clock.now().tzinfo is clock.KYIV
    assert clock.kyiv_date(datetime.now(timezone.utc)) == clock.today()
