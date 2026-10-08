"""The plug's own clock, read off the device info it already reports.

Rules are wall-clock times the device fires itself. A plug set to another
region therefore shifts every rule by the offset, and nothing else in the chain
would look wrong -- the calendar, the diff and the post would all agree with
each other. This is the one failure the rest of the program cannot see.
"""

from __future__ import annotations

from tapo_scheduler.plug import clock_warning


def test_a_plug_on_kyiv_is_left_alone() -> None:
    assert clock_warning({"region": "Europe/Kyiv", "time_diff": 120}) is None


def test_another_region_is_reported() -> None:
    warning = clock_warning({"region": "America/New_York", "time_diff": -300})
    assert warning is not None
    assert "America/New_York" in warning
    assert "Europe/Kyiv" in warning


def test_the_offset_is_quoted_as_context() -> None:
    """`time_diff` is minutes east of UTC, so the message has to convert it."""
    assert "-5" in (clock_warning({"region": "America/New_York", "time_diff": -300}) or "")


def test_a_plug_that_says_nothing_is_not_guessed_about() -> None:
    """An older firmware may omit the field; that is not evidence of a fault."""
    assert clock_warning({}) is None


def test_an_empty_region_is_not_a_fault() -> None:
    assert clock_warning({"region": ""}) is None


def test_a_missing_offset_still_reports_the_region() -> None:
    warning = clock_warning({"region": "America/New_York"})
    assert warning is not None
    assert "America/New_York" in warning
