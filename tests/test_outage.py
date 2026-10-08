"""Regression tests for the ГПВ parser.

The fixtures are real posts, verbatim, because the whole risk here is that the
operator's wording drifts and the parser silently starts returning None.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from tapo_scheduler.outage import (
    MINUTES_PER_DAY,
    OFF_LEAD_MINUTES,
    ON_LAG_MINUTES,
    Calendar,
    Window,
    format_minute,
    merge_windows,
    mirror_rules,
    parse_schedule,
    schedules_by_date,
)
from tapo_scheduler.telegram import Post

TODAY = date(2026, 10, 8)

REVISION_POST = """Оновлений графік погодинних відключень (ГПВ) на 8 жовтня.

Години відсутності електропостачання:

1.1 11:00 - 13:00, 17:00 - 19:00, 21:00 - 23:00

1.2 07:00 - 09:00, 11:00 - 14:00, 17:00 - 19:00, 21:00 - 23:00

2.1 07:00 - 09:00, 11:00 - 14:00, 17:00 - 19:00, 23:00 - 00:00

2.2 11:00 - 13:00, 15:00 - 17:00, 19:00 - 21:00, 23:00 - 00:00

3.1 08:00 - 11:00, 14:00 - 17:00, 19:00 - 21:00

3.2 08:00 - 11:00, 14:00 - 17:00

4.1 07:00 - 10:00, 13:00 - 15:00, 17:00 - 19:00, 21:00 - 22:00

4.2 07:00 - 10:00, 13:00 - 15:00, 17:00 - 19:00, 21:00 - 23:00

5.1 09:00 - 11:00, 13:00 - 15:00, 21:00 - 23:00

5.2 09:00 - 11:00, 13:00 - 15:00, 19:00 - 21:00, 23:00 - 00:00

6.1 10:00 - 13:00, 15:00 - 17:00, 19:00 - 21:00

6.2 10:00 - 13:00, 15:00 - 17:00, 19:00 - 21:00, 23:00 - 00:00

Перелік адрес, що знеструмлюються по чергах (підчергах) ГПВ можна переглянути
за посиланням https://www.cherkasyoblenergo.com/off
"""

# The first announcement of a day drops the preposition before the date.
ANNOUNCEMENT_POST = """За розпорядженням НЕК «Укренерго» 8 жовтня з 00:00 до 24:00 у
Черкаській області будуть застосовані графіки погодинних відключень (ГПВ).

Години відсутності електропостачання:

1.2 00:00 - 01:00, 17:00 - 19:00

3.1 09:00 - 11:00, 15:00 - 17:00
"""

# Industry-only power-limiting notice: mentions a date and a command, but
# schedules nothing per queue.
GOP_POST = """Відповідно до команди НЕК "Укренерго" для промисловості та бізнесу
Черкаської області 8 жовтня з 00:00 до 24:00 будуть застосовані графіки
обмеження потужності (ГОП)."""


def test_revision_post_parses_every_queue() -> None:
    schedule = parse_schedule(REVISION_POST, today=TODAY)
    assert schedule is not None
    assert schedule.date == date(2026, 10, 8)
    assert len(schedule.queues) == 12
    assert schedule.windows("3.1") == (
        Window(8 * 60, 11 * 60),
        Window(14 * 60, 17 * 60),
        Window(19 * 60, 21 * 60),
    )


def test_bare_date_announcement_parses() -> None:
    schedule = parse_schedule(ANNOUNCEMENT_POST, today=TODAY)
    assert schedule is not None
    assert schedule.date == date(2026, 10, 8)
    assert schedule.windows("1.2") == (Window(0, 60), Window(17 * 60, 19 * 60))


def test_gop_notice_is_not_a_schedule() -> None:
    assert parse_schedule(GOP_POST, today=TODAY) is None


def test_empty_text_is_not_a_schedule() -> None:
    assert parse_schedule("", today=TODAY) is None


def test_late_december_post_dates_into_next_year() -> None:
    schedule = parse_schedule(
        "Оновлений графік погодинних відключень (ГПВ) на 3 січня.\n\n1.1 10:00 - 12:00",
        today=date(2026, 12, 30),
    )
    assert schedule is not None
    assert schedule.date == date(2027, 1, 3)


def test_midnight_end_is_end_of_day_not_a_wrap() -> None:
    schedule = parse_schedule(REVISION_POST, today=TODAY)
    assert schedule is not None
    # "23:00 - 00:00" must reach 24:00, not stop at zero.
    assert schedule.windows("2.1")[-1] == Window(23 * 60, MINUTES_PER_DAY)


def test_touching_windows_fuse() -> None:
    assert merge_windows([Window(660, 780), Window(780, 900)]) == (Window(660, 900),)


def test_overlapping_windows_fuse() -> None:
    assert merge_windows([Window(660, 800), Window(780, 900)]) == (Window(660, 900),)


def pairs(rules: tuple) -> list[tuple[int, str]]:
    return [(rule.minute, rule.state) for rule in rules]


def test_no_outages_yields_no_rules() -> None:
    assert mirror_rules(()) == ()


def test_outage_all_day_yields_a_single_off() -> None:
    assert pairs(mirror_rules((Window(0, MINUTES_PER_DAY),))) == [(0, "OFF")]


def test_mirror_rules_bracket_the_outage() -> None:
    """With no margins the transitions land exactly on the announced hours."""
    rules = mirror_rules((Window(11 * 60, 13 * 60),), lead=0, lag=0)
    assert pairs(rules) == [(11 * 60, "OFF"), (13 * 60, "ON")]


def test_lead_cuts_power_early_and_lag_restores_it_late() -> None:
    rules = mirror_rules((Window(11 * 60, 13 * 60),))
    assert pairs(rules) == [
        (11 * 60 - OFF_LEAD_MINUTES, "OFF"),
        (13 * 60 + ON_LAG_MINUTES, "ON"),
    ]


def test_lag_wraps_past_midnight() -> None:
    """An outage ending at 24:00 must come back on 20 minutes into the next day."""
    rules = mirror_rules((Window(23 * 60, MINUTES_PER_DAY),))
    assert pairs(rules) == [(ON_LAG_MINUTES, "ON"), (23 * 60 - OFF_LEAD_MINUTES, "OFF")]


def test_lead_wraps_back_before_midnight() -> None:
    """An outage starting at 00:00 must be cut 5 minutes before, i.e. at 23:55."""
    rules = mirror_rules((Window(0, 60),))
    assert pairs(rules) == [
        (60 + ON_LAG_MINUTES, "ON"),
        (MINUTES_PER_DAY - OFF_LEAD_MINUTES, "OFF"),
    ]


def test_outages_closer_than_the_margins_fuse() -> None:
    """No point switching on for a gap barely longer than the margins."""
    rules = mirror_rules((Window(11 * 60, 11 * 60 + 10), Window(11 * 60 + 20, 12 * 60)))
    assert pairs(rules) == [(11 * 60 - OFF_LEAD_MINUTES, "OFF"), (12 * 60 + ON_LAG_MINUTES, "ON")]


def test_day_fully_covered_by_margins_is_a_single_off() -> None:
    rules = mirror_rules((Window(0, MINUTES_PER_DAY),), lead=60, lag=60)
    assert pairs(rules) == [(0, "OFF")]


@pytest.mark.parametrize(
    ("minute", "expected"),
    [(0, "00:00"), (60, "01:00"), (13 * 60 + 5, "13:05"), (MINUTES_PER_DAY, "24:00")],
)
def test_format_minute(minute: int, expected: str) -> None:
    assert format_minute(minute) == expected


# --- calendar ---------------------------------------------------------------


def post(pid: int, text: str) -> Post:
    return Post(
        id=pid,
        channel="test",
        published=datetime(2026, 10, 7, 16, 0, tzinfo=timezone.utc),
        text=text,
        has_photo=False,
    )


def revision(day: int, windows: str) -> str:
    return (
        f"Оновлений графік погодинних відключень (ГПВ) на {day} жовтня.\n\n"
        f"Години відсутності електропостачання:\n\n1.1 {windows}\n"
    )


def test_newest_revision_wins_per_date() -> None:
    by_date = schedules_by_date(
        [
            post(1, revision(8, "07:00 - 09:00")),
            post(2, revision(8, "11:00 - 13:00")),
            post(3, revision(9, "15:00 - 17:00")),
        ],
        today=TODAY,
    )
    assert sorted(by_date) == [date(2026, 10, 8), date(2026, 10, 9)]
    assert by_date[date(2026, 10, 8)].windows("1.1") == (Window(11 * 60, 13 * 60),)


def test_calendar_splits_today_from_tomorrow() -> None:
    calendar = Calendar.from_schedules(
        schedules_by_date(
            [post(1, revision(8, "07:00 - 09:00")), post(2, revision(9, "15:00 - 17:00"))],
            today=TODAY,
        ),
        TODAY,
    )
    assert calendar.today is not None and calendar.today.date == TODAY
    assert calendar.tomorrow is not None and calendar.tomorrow.date == date(2026, 10, 9)
    assert calendar.for_date(TODAY) is calendar.today


def test_calendar_tolerates_a_missing_tomorrow() -> None:
    """Tomorrow is published only sometimes; that is not an error."""
    calendar = Calendar.from_schedules(
        schedules_by_date([post(1, revision(8, "07:00 - 09:00"))], today=TODAY), TODAY
    )
    assert calendar.today is not None
    assert calendar.tomorrow is None
    assert calendar.for_date(date(2026, 10, 9)) is None


def test_calendar_reports_a_missing_today() -> None:
    """A missing today is the alarm case -- callers must not read it as calm."""
    calendar = Calendar.from_schedules({}, TODAY)
    assert calendar.today is None
