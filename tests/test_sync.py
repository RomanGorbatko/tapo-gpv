"""Tests for the sync diff.

The diff is the load-bearing part: sync runs every minute, and a plug's schedule
lives in config flash. Rewriting unconditionally would wear that flash out, so
"already correct means touch nothing" is a correctness property, not an
optimisation.
"""

from __future__ import annotations

import asyncio

import pytest
from tapo.requests import DaysOfWeek, ScheduleTime

from tapo_scheduler.config import Config, PlugTarget
from tapo_scheduler.outage import Rule
from tapo_scheduler.sync import (
    EVERY_DAY_BITS,
    PassLog,
    apply_to_plug,
    desired_keys,
    select_targets,
    to_device_rules,
    warn_on_foreign_clock,
)

EVERY_DAY = DaysOfWeek.EVERY_DAY.bits()


class _Days:
    def __init__(self, bits: int = EVERY_DAY_BITS):
        self._bits = bits

    def bits(self) -> int:
        return self._bits

    def is_empty(self) -> bool:
        return self._bits == 0


class FakeRule:
    """Stand-in for a `ScheduleRuleResult` read back off a device.

    Carries the same attributes `plug.rule_key` and `plug.format_rule` read.
    """

    id = "S1"

    def __init__(self, time, *, on: bool = False, enabled: bool = True, bits: int = EVERY_DAY_BITS):
        self.time = time
        self.desired_state = "On" if on else "Off"
        self.enabled = enabled
        self.days = _Days(bits)


def clock_rule(minute: int, on: bool, **kwargs) -> FakeRule:
    return FakeRule(ScheduleTime.Clock(minute // 60, minute % 60), on=on, **kwargs)


def sunrise_rule() -> FakeRule:
    return FakeRule(ScheduleTime.Sunrise(0), on=False)


class _Info:
    def __init__(self, raw):
        self.raw = raw


class FakePlug:
    """A plug that records what it was asked to do.

    `readback` is what the device reports *after* a write -- real `ScheduleRule`
    objects are write-only, so the device's answer has to be supplied rather
    than derived from what was sent.
    """

    def __init__(self, rules=(), readback=None, region="Europe/Kyiv"):
        self.ip = "192.168.68.62"
        self._rules = list(rules)
        self.readback = list(readback) if readback is not None else None
        self.written: list[object] = []
        self.cleared = 0
        # `connect` fetches this before anything else, and `clock_warning`
        # reads it. `time_diff` is the standard-offset field these devices
        # report -- 120 for Kyiv, even in summer.
        self._info = _Info({"region": region, "time_diff": 120})

    async def info(self):
        return self._info

    async def schedule_rules(self):
        return list(self._rules)

    async def clear_schedule(self) -> None:
        self.cleared += 1
        self._rules = list(self.readback) if self.readback is not None else []

    async def add_schedule_rule(self, rule) -> None:
        self.written.append(rule)


ACTIONS = (Rule(11 * 60 - 5, False), Rule(13 * 60 + 20, True))

# A device holding exactly what ACTIONS describes.
IN_SYNC = [clock_rule(minute, on) for minute, on, _, _ in desired_keys(ACTIONS)]


def apply(plug: FakePlug, actions=ACTIONS, *, dry_run: bool = False) -> bool:
    return asyncio.run(apply_to_plug(plug, actions, dry_run=dry_run))


def test_to_device_rules_builds_one_per_action() -> None:
    assert len(to_device_rules(ACTIONS)) == 2


def test_desired_keys_marks_rules_enabled_and_daily() -> None:
    assert desired_keys(ACTIONS) == [
        (11 * 60 - 5, False, True, EVERY_DAY_BITS),
        (13 * 60 + 20, True, True, EVERY_DAY_BITS),
    ]


def test_matching_rules_are_left_alone() -> None:
    """The flash-wear guard: identical rules must not trigger a rewrite."""
    plug = FakePlug(rules=IN_SYNC)
    assert apply(plug) is False
    assert plug.cleared == 0
    assert plug.written == []


def test_stale_rules_are_replaced() -> None:
    """Yesterday's schedule is a different rule set, so the plug is reloaded."""
    plug = FakePlug(rules=[clock_rule(13 * 60 + 5, False)], readback=IN_SYNC)
    assert apply(plug) is True
    assert plug.cleared == 1
    assert len(plug.written) == 2


def test_a_disabled_rule_does_not_count_as_matching() -> None:
    """An identical rule the user switched off by hand must come back on."""
    plug = FakePlug(
        rules=[clock_rule(minute, on, enabled=False) for minute, on, _, _ in desired_keys(ACTIONS)],
        readback=IN_SYNC,
    )
    assert apply(plug) is True
    assert plug.cleared == 1


def test_a_sunrise_rule_never_matches_a_clock_rule() -> None:
    """Leftover sunrise rules must be swept away, not treated as equivalent."""
    plug = FakePlug(rules=[sunrise_rule()], readback=IN_SYNC)
    assert apply(plug) is True
    assert plug.cleared == 1


def test_dry_run_reports_but_writes_nothing() -> None:
    plug = FakePlug(rules=[clock_rule(0, False)])
    assert apply(plug, dry_run=True) is True
    assert plug.cleared == 0
    assert plug.written == []


def test_a_device_that_drops_a_rule_is_an_error() -> None:
    """Silently ending up with half a schedule would be worse than failing."""
    plug = FakePlug(rules=[clock_rule(0, False)], readback=[clock_rule(11 * 60 - 5, False)])
    with pytest.raises(RuntimeError, match="did not accept the rules"):
        apply(plug)


def test_no_outages_clears_the_plug() -> None:
    """An outage-free day at --assume-no-outages must not leave stale rules."""
    plug = FakePlug(rules=[clock_rule(0, False)], readback=[])
    assert apply(plug, ()) is True
    assert plug.cleared == 1
    assert plug.written == []


# --- logging ----------------------------------------------------------------


def test_a_quiet_pass_holds_its_chatter(capsys) -> None:
    """The watcher runs every minute; the routine lines are the same each time."""
    log = PassLog()
    log.say("calendar : today 12 queues")
    log.say("192.168.68.62  queue 5.1  (6 rules)")
    log.say("    unchanged")
    assert capsys.readouterr().out == ""

    log.flush()
    assert "unchanged" in capsys.readouterr().out


def test_a_changed_pass_releases_what_it_held(capsys) -> None:
    log = PassLog()
    plug = FakePlug(rules=[clock_rule(0, False)], readback=IN_SYNC)
    assert asyncio.run(apply_to_plug(plug, ACTIONS, dry_run=False, log=log)) is True
    assert capsys.readouterr().out == ""

    log.flush()
    out = capsys.readouterr().out
    assert "was" in out and "now" in out


def test_a_one_off_run_says_everything_immediately(capsys) -> None:
    """A single run is being watched; silence would look like it never ran."""
    log = PassLog(always=True)
    log.say("    unchanged")
    assert "unchanged" in capsys.readouterr().out


def test_apply_without_a_log_still_prints(capsys) -> None:
    plug = FakePlug(rules=[clock_rule(0, False)], readback=IN_SYNC)
    asyncio.run(apply_to_plug(plug, ACTIONS, dry_run=False))
    assert "was" in capsys.readouterr().out


# --- target selection -------------------------------------------------------


def config_with(*targets: PlugTarget) -> Config:
    return Config(email="a@b.c", password="x", targets=list(targets))


def test_select_targets_refuses_a_plug_without_a_queue() -> None:
    config = config_with(PlugTarget("192.168.68.62", "3.1"), PlugTarget("192.168.68.63"))
    assert select_targets(config, None) is None


def test_select_targets_keeps_configured_queues() -> None:
    config = config_with(
        PlugTarget("192.168.68.62", "3.1"), PlugTarget("192.168.68.63", "6.2")
    )
    assert [str(target) for target in select_targets(config, None)] == [
        "192.168.68.62:3.1",
        "192.168.68.63:6.2",
    ]


def test_select_targets_narrows_by_ip() -> None:
    config = config_with(
        PlugTarget("192.168.68.62", "3.1"), PlugTarget("192.168.68.63", "6.2")
    )
    assert [str(target) for target in select_targets(config, ["192.168.68.63"])] == [
        "192.168.68.63:6.2"
    ]


def test_select_targets_rejects_an_unknown_ip() -> None:
    config = config_with(PlugTarget("192.168.68.62", "3.1"))
    assert select_targets(config, ["192.168.68.99"]) is None


def test_select_targets_needs_something_to_do() -> None:
    assert select_targets(config_with(), None) is None


# --- the plug's own clock --------------------------------------------------
#
# Rules are wall-clock times the device fires itself, so a plug on another
# region shifts every one of them. The calendar, the diff and the post would
# all keep agreeing with each other while the boiler ran at the wrong hours,
# which is why this is worth interrupting a quiet pass for.


def warn(plug: FakePlug, checked: set[str] | None = None) -> set[str]:
    checked = set() if checked is None else checked
    asyncio.run(warn_on_foreign_clock(plug, checked))
    return checked


def test_a_plug_on_another_region_is_reported(capsys) -> None:
    warn(FakePlug(region="America/New_York"))
    err = capsys.readouterr().err
    assert "America/New_York" in err
    assert "Europe/Kyiv" in err
    assert FakePlug().ip in err


def test_a_plug_is_reported_once_per_process(capsys) -> None:
    """The watcher runs every minute; a standing warning would bury the log."""
    plug = FakePlug(region="America/New_York")
    checked = warn(plug)
    warn(plug, checked)
    assert capsys.readouterr().err.count("America/New_York") == 1


def test_two_plugs_are_each_reported(capsys) -> None:
    """The dedupe is per IP, not a single "already warned" flag."""
    first = FakePlug(region="America/New_York")
    second = FakePlug(region="America/New_York")
    second.ip = "192.168.68.63"
    checked = warn(first)
    warn(second, checked)
    assert capsys.readouterr().err.count("America/New_York") == 2


def test_a_plug_on_kyiv_time_is_not_reported(capsys) -> None:
    warn(FakePlug())
    assert capsys.readouterr().err == ""
