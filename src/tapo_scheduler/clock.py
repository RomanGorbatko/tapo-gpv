"""The one clock the program is allowed to ask.

ГПВ schedules are published under a Kyiv calendar date, and the plugs fire
their rules on their own Kyiv wall clock. The machine running the sync,
however, may sit anywhere: a container defaults to UTC, a server can be set
to whatever its operator liked. Asking the host what day it is would answer a
different question than the one that matters -- at 22:30 UTC it is already
01:30 of the next day in Kyiv, so a UTC host would hand back yesterday's
schedule and the boiler would run straight through a real outage.

So "now" is resolved against Europe/Kyiv explicitly and never against the
host. Nothing here is configurable on purpose: which timezone this program
means is a property of the data it consumes, not a deployment preference.

Setting ``TZ=Europe/Kyiv`` in the container is still worth doing, but only as
a backstop -- it keeps third-party logging and any stray ``datetime.now()``
in step. Nothing in this module depends on it.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")


def kyiv_date(at: datetime) -> date:
    """The Kyiv calendar date an instant falls on.

    Split out from ``today`` so the day boundary and the DST transitions can be
    tested against a fixed instant instead of against whatever time it is now.
    """
    return at.astimezone(KYIV).date()


def now() -> datetime:
    """Current time in Kyiv, timezone-aware."""
    return datetime.now(KYIV)


def today() -> date:
    """Today in Kyiv -- the date a ГПВ schedule is published under."""
    return now().date()
