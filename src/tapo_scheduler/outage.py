"""Parse the operator's ГПВ post into per-queue outage windows.

The posts look like this (queue rows, one per sub-queue)::

    Оновлений графік погодинних відключень (ГПВ) на 8 жовтня.

    Години відсутності електропостачання:

    1.1 11:00 - 13:00, 17:00 - 19:00, 21:00 - 23:00
    3.1 08:00 - 11:00, 14:00 - 17:00, 19:00 - 21:00
    6.2 10:00 - 13:00, 15:00 - 17:00, 19:00 - 21:00, 23:00 - 00:00

A window ending at ``00:00`` or ``24:00`` means end of day, not a wrap into the
next one -- the operator splits overnight outages at midnight instead.

Two transitions per outage window keep a plug powered exactly outside it: OFF
before the outage starts, ON after it ends. Because the rules that go on the
plug repeat every day, the day is treated as a cycle, so ``23:00 - 00:00``
still yields an ON after midnight.

The margins are not symmetric. Power is cut ``OFF_LEAD_MINUTES`` *before* the
announced start, so the plug is already off when the grid drops rather than
dying mid-draw; and restored ``ON_LAG_MINUTES`` *after* the announced end, so
the boiler does not join the surge of everything else switching back on the
instant power returns.

Limits worth designing around:

* Only the planned ГПВ is covered. Emergency outages are announced without
  per-queue hours, so no rule can mirror them -- the plug stays wherever its
  schedule left it.
* The operator sometimes posts an infographic with no text. Nothing is
  parseable there, so callers must treat "no schedule found" as a signal to
  keep the previous rules *and* raise an alarm, not as "no outages today".
* The schedules for two consecutive days routinely disagree about the same
  wall-clock hours, and every rule on the plug repeats daily, so only one day
  can be loaded at a time. `Calendar` holds both; the plug gets today's.

    python -m tapo_scheduler.outage                       # newest schedule
    python -m tapo_scheduler.outage --queue 3.1           # mirror rules for 3.1
    python -m tapo_scheduler.outage --date 2026-10-08 --queue 3.1 --json

Note the numbering: the operator publishes 12 *sub-queues* (1.1, 1.2 ... 6.2),
not 6. Which one a given address sits on comes from
https://www.cherkasyoblenergo.com/off or the Cherkasyoblenergo_bot chat bot.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, timedelta

from . import clock
from .telegram import DEFAULT_CHANNEL, Post, fetch_posts

MINUTES_PER_DAY = 24 * 60

# Margins around every announced outage, in minutes. See the module docstring.
OFF_LEAD_MINUTES = 5
ON_LAG_MINUTES = 20

# Ukrainian months in the genitive, which is what "на 8 жовтня" uses. The
# nominative forms are accepted too, in case the operator changes style.
MONTHS = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4,
    "травня": 5, "червня": 6, "липня": 7, "серпня": 8,
    "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
    "січень": 1, "лютий": 2, "березень": 3, "квітень": 4,
    "травень": 5, "червень": 6, "липень": 7, "серпень": 8,
    "вересень": 9, "жовтень": 10, "листопад": 11, "грудень": 12,
}

# The anchor that says which day the schedule is for. Revisions are titled
# "Оновлений графік ... на 8 жовтня"; the first announcement of a day is titled
# "За розпорядженням ... 8 жовтня ..." and drops the preposition, so "на" is
# optional -- but the form that has it wins, because it is unambiguous.
DATE_WITH_NA_RE = re.compile(r"\bна\s+(\d{1,2})\s+([А-Яа-яЇїІіЄєҐґ']+)")
DATE_BARE_RE = re.compile(r"\b(\d{1,2})\s+([А-Яа-яЇїІіЄєҐґ']+)")

# "11:00 - 13:00". Any of the dash characters Telegram users type in practice.
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[-‐‑‒–—―]\s*(\d{1,2}):(\d{2})")

# "3.1 08:00 - 11:00, ..." -- sub-queue id, then its windows. Some posts use a
# bare "3" instead of "3.1".
QUEUE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s+(.*\S)\s*$")

# A post only counts as a schedule if it carries this marker. Without it we
# would happily parse the "графіки обмеження потужності" notices, which list no
# per-queue hours at all.
SCHEDULE_MARKER_RE = re.compile(r"ГПВ|погодинних відключень", re.IGNORECASE)


# Nominative/genitive month names in index order, for rendering dates the way
# the operator writes them ("8 жовтня").
MONTH_NAMES_GENITIVE = (
    "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
)


def format_date_uk(when: date) -> str:
    """Render a date as the operator would: ``8 жовтня``."""
    return f"{when.day} {MONTH_NAMES_GENITIVE[when.month - 1]}"


def format_minute(minute: int) -> str:
    """Render minutes-from-midnight as HH:MM, keeping 1440 readable as 24:00."""
    if minute == MINUTES_PER_DAY:
        return "24:00"
    return f"{minute // 60:02d}:{minute % 60:02d}"


@dataclass(frozen=True, order=True)
class Window:
    """A power outage, in minutes from midnight. ``end`` is exclusive."""

    start: int
    end: int

    def __str__(self) -> str:
        return f"{format_minute(self.start)} - {format_minute(self.end)}"

    def to_dict(self) -> dict[str, object]:
        return {
            "start": format_minute(self.start),
            "end": format_minute(self.end),
            "start_minute": self.start,
            "end_minute": self.end,
        }


@dataclass(frozen=True)
class Rule:
    """One scheduled transition: at ``minute`` set the plug to ``on``."""

    minute: int
    on: bool

    @property
    def state(self) -> str:
        return "ON" if self.on else "OFF"

    def __str__(self) -> str:
        return f"{format_minute(self.minute)}  {self.state}"

    def to_dict(self) -> dict[str, object]:
        return {"time": format_minute(self.minute), "minute": self.minute, "state": self.state}


@dataclass(frozen=True)
class OutageSchedule:
    date: date
    queues: dict[str, tuple[Window, ...]]
    source: Post | None = None

    def windows(self, queue: str) -> tuple[Window, ...]:
        try:
            return self.queues[queue]
        except KeyError:
            known = ", ".join(sorted(self.queues, key=_queue_sort_key))
            raise KeyError(f"unknown queue {queue!r}; post lists: {known}") from None

    def mirror_rules(
        self,
        queue: str,
        *,
        lead: int = OFF_LEAD_MINUTES,
        lag: int = ON_LAG_MINUTES,
    ) -> tuple[Rule, ...]:
        """Transitions that keep the plug powered exactly outside its outages."""
        return mirror_rules(self.windows(queue), lead=lead, lag=lag)

    def to_dict(self) -> dict[str, object]:
        return {
            "date": self.date.isoformat(),
            "source": self.source.url if self.source else None,
            "queues": {
                queue: [window.to_dict() for window in windows]
                for queue, windows in sorted(self.queues.items(), key=lambda kv: _queue_sort_key(kv[0]))
            },
        }


def _queue_sort_key(queue: str) -> tuple[int, int]:
    major, _, minor = queue.partition(".")
    return (int(major), int(minor or 0))


def _parse_clock(hour: int, minute: int) -> int:
    if hour == 24 and minute == 0:
        return MINUTES_PER_DAY
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"time out of range: {hour:02d}:{minute:02d}")
    return hour * 60 + minute


def _normalise(start: int, end: int) -> list[Window]:
    """Turn one parsed range into windows inside a single day.

    ``23:00 - 00:00`` reads as end-of-day, so a non-positive span means the
    author meant midnight rather than a wrap. Anything that still spills past
    midnight is split, so every window stays within 00:00-24:00.
    """
    if end <= start:
        end = MINUTES_PER_DAY
    if start >= MINUTES_PER_DAY:
        return []
    if end > MINUTES_PER_DAY:
        return [Window(start, MINUTES_PER_DAY), Window(0, end - MINUTES_PER_DAY)]
    return [Window(start, end)]


def merge_windows(windows: list[Window]) -> tuple[Window, ...]:
    """Sort and fuse overlapping or touching windows.

    Fusing matters: two outages that meet at 13:00 leave no live gap between
    them, so they must not produce an ON immediately followed by an OFF.
    """
    if not windows:
        return ()
    ordered = sorted(windows)
    merged = [ordered[0]]
    for window in ordered[1:]:
        last = merged[-1]
        if window.start <= last.end:
            merged[-1] = Window(last.start, max(last.end, window.end))
        else:
            merged.append(window)
    return tuple(merged)


def _mark_out(windows: tuple[Window, ...]) -> bytearray:
    """Per-minute map of when the grid is expected to be down.

    Windows are taken modulo the day, so a window may start before 00:00 or run
    past 24:00 -- which is exactly what the margins produce around midnight.
    Overlapping windows simply union, so no merge pass is needed.
    """
    out = bytearray(MINUTES_PER_DAY)
    for window in windows:
        span = window.end - window.start
        if span <= 0:
            continue
        if span >= MINUTES_PER_DAY:
            return bytearray(b"\x01" * MINUTES_PER_DAY)
        for minute in range(window.start, window.end):
            out[minute % MINUTES_PER_DAY] = 1  # negative indices wrap, as wanted
    return out


def mirror_rules(
    windows: tuple[Window, ...],
    *,
    lead: int = OFF_LEAD_MINUTES,
    lag: int = ON_LAG_MINUTES,
) -> tuple[Rule, ...]:
    """Rules that power the plug exactly when the grid does not.

    Each outage is widened by ``lead`` before its start and ``lag`` after its
    end, then the day is walked as a cycle. Walking cyclically is what makes an
    outage running to 24:00 still produce the ON that brings the plug back
    after midnight.

    Two outages separated by less than ``lead + lag`` fuse into one, which is
    the point: there is no window long enough to be worth switching back on
    for, so the plug stays off across both.
    """
    widened = tuple(
        Window(window.start - lead, window.end + lag)
        for window in merge_windows(list(windows))
    )
    out = _mark_out(widened)

    if not any(out):
        return ()  # no outages -- leave the plug alone
    if all(out):
        return (Rule(0, False),)  # out all day -- one OFF, no ON

    rules = []
    for minute in range(MINUTES_PER_DAY):
        previous = out[minute - 1]  # wraps to 23:59 when minute is 0
        if out[minute] != previous:
            rules.append(Rule(minute, not out[minute]))
    return tuple(rules)


def parse_schedule(
    text: str, *, today: date | None = None, source: Post | None = None
) -> OutageSchedule | None:
    """Parse one post. Returns None when it does not describe a ГПВ schedule."""
    if not text or not SCHEDULE_MARKER_RE.search(text):
        return None

    match = DATE_WITH_NA_RE.search(text) or DATE_BARE_RE.search(text)
    if match is None:
        return None
    month = MONTHS.get(match.group(2).lower())
    if month is None:
        return None

    today = today or clock.today()
    try:
        when = date(today.year, month, int(match.group(1)))
    except ValueError:
        return None
    # A post in late December announcing "на 3 січня" means next year, not the
    # one that already passed.
    if when < today - timedelta(days=180):
        when = date(today.year + 1, month, int(match.group(1)))

    queues: dict[str, tuple[Window, ...]] = {}
    for line in text.splitlines():
        row = QUEUE_RE.match(line)
        if row is None:
            continue
        queue, rest = row.group(1), row.group(2)
        found: list[Window] = []
        for hour, minute, end_hour, end_minute in TIME_RE.findall(rest):
            try:
                start_at = _parse_clock(int(hour), int(minute))
                end_at = _parse_clock(int(end_hour), int(end_minute))
            except ValueError:
                continue
            found.extend(_normalise(start_at, end_at))
        if found:
            queues[queue] = merge_windows(found)

    if not queues:
        return None
    return OutageSchedule(date=when, queues=queues, source=source)


def schedules_by_date(posts: list[Post], *, today: date | None = None) -> dict[date, OutageSchedule]:
    """Newest revision per date, keyed by the date it describes.

    The operator revises a day's schedule several times and sometimes announces
    tomorrow's in the evening, so what matters is not the newest *post* but the
    newest post *for each date*. ``posts`` must be oldest-first, which is the
    order `fetch_posts` returns.
    """
    by_date: dict[date, OutageSchedule] = {}
    for post in posts:
        schedule = parse_schedule(post.text, today=today, source=post)
        if schedule is not None:
            by_date[schedule.date] = schedule  # later posts overwrite earlier
    return by_date


def collect_schedules(
    channel: str = DEFAULT_CHANNEL, *, posts: int = 40, today: date | None = None
) -> dict[date, OutageSchedule]:
    return schedules_by_date(fetch_posts(channel, limit=posts), today=today)


@dataclass(frozen=True)
class Calendar:
    """Both schedules that matter: the day in effect, and the next one.

    Today's is always expected -- when it is missing something is wrong, and
    the caller must keep whatever is already on the plugs rather than treat the
    silence as "no outages". Tomorrow's is opportunistic: the operator usually
    publishes it in the evening, and having it in hand is what makes the
    rollover at midnight immediate instead of dependent on a fresh fetch.
    """

    today: OutageSchedule | None
    tomorrow: OutageSchedule | None

    @classmethod
    def from_schedules(
        cls, by_date: dict[date, OutageSchedule], today: date
    ) -> Calendar:
        return cls(
            today=by_date.get(today),
            tomorrow=by_date.get(today + timedelta(days=1)),
        )

    def for_date(self, when: date) -> OutageSchedule | None:
        if self.today is not None and self.today.date == when:
            return self.today
        if self.tomorrow is not None and self.tomorrow.date == when:
            return self.tomorrow
        return None

    def __str__(self) -> str:
        def describe(schedule: OutageSchedule | None) -> str:
            if schedule is None:
                return "missing"
            return f"{len(schedule.queues)} queues  ({schedule.source.url if schedule.source else '?'})"

        return f"today {self.today.date if self.today else '?'}: {describe(self.today)}\n" \
               f"tomorrow: {describe(self.tomorrow)}"


def load_calendar(
    channel: str = DEFAULT_CHANNEL,
    *,
    posts: int = 40,
    today: date | None = None,
) -> Calendar:
    """Fetch the schedules for today and tomorrow.

    ``today`` defaults to today in Kyiv -- the wall clock the operator
    publishes under and the plugs fire on, wherever this machine happens to
    sit. Pass it explicitly to make a run reproducible.
    """
    today = today or clock.today()
    return Calendar.from_schedules(
        collect_schedules(channel, posts=posts, today=today), today
    )


# --- CLI -------------------------------------------------------------------


def _report_queue(schedule: OutageSchedule, queue: str, lead: int, lag: int) -> None:
    outages = schedule.windows(queue)
    actions = schedule.mirror_rules(queue, lead=lead, lag=lag)
    print(f"schedule : {schedule.date}  (queue {queue})")
    print(f"source   : {schedule.source.url if schedule.source else '?'}")
    print(f"outages  : {len(outages)}  (as announced)")
    for window in outages:
        print(f"    {window}")
    print(f"plug     : {len(actions)} rules  (off {lead} min before, on {lag} min after)")
    for rule in actions:
        print(f"    {rule}")


def _report_all(schedule: OutageSchedule) -> None:
    print(f"schedule : {schedule.date}")
    print(f"source   : {schedule.source.url if schedule.source else '?'}")
    print(f"queues   : {len(schedule.queues)}")
    for queue in sorted(schedule.queues, key=_queue_sort_key):
        windows = ", ".join(str(window) for window in schedule.queues[queue])
        print(f"    {queue:<5} {windows}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default=DEFAULT_CHANNEL, help="public channel username")
    parser.add_argument(
        "--date", help="YYYY-MM-DD to treat as today; defaults to today in Kyiv"
    )
    parser.add_argument("--queue", help="sub-queue id, e.g. 3.1; omit to list every queue")
    parser.add_argument("--posts", type=int, default=40, help="how far back to read")
    parser.add_argument("--lead", type=int, default=OFF_LEAD_MINUTES, help="minutes early to cut")
    parser.add_argument("--lag", type=int, default=ON_LAG_MINUTES, help="minutes late to restore")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    on = date.fromisoformat(args.date) if args.date else None

    # `on` goes into the fetch, not just the lookup: the parser resolves
    # "сьогодні" and bare dates against the day it is told is today, so passing
    # it here is what makes `--date` mean the day it names rather than merely
    # selecting among what happened to load for the real current day.
    calendar = load_calendar(args.channel, posts=args.posts, today=on)
    schedule = calendar.for_date(on) if on else calendar.today
    if schedule is None:
        where = f" for {on}" if on else " for today"
        print(f"no ГПВ schedule found{where} in the last {args.posts} posts", file=sys.stderr)
        if calendar.tomorrow is not None:
            print(f"(tomorrow's is there: {calendar.tomorrow.source.url if calendar.tomorrow.source else '?'})", file=sys.stderr)
        return 1

    if args.json:
        payload: dict[str, object] = schedule.to_dict()
        payload["lead"] = args.lead
        payload["lag"] = args.lag
        payload["tomorrow"] = calendar.tomorrow.date.isoformat() if calendar.tomorrow else None
        if args.queue:
            payload["queue"] = args.queue
            payload["plug_rules"] = [
                rule.to_dict() for rule in schedule.mirror_rules(args.queue, lead=args.lead, lag=args.lag)
            ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.queue:
        _report_queue(schedule, args.queue, args.lead, args.lag)
    else:
        _report_all(schedule)
        print(f"tomorrow : {calendar.tomorrow.date if calendar.tomorrow else 'not posted yet'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
