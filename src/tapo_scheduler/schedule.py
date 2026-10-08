"""Read and write the schedule rules stored on the plugs.

The device is the source of truth at runtime: rules keep firing while the Mac,
the router or the Tapo cloud are down. This CLI is how they get there.

    python -m tapo_scheduler.schedule list
    python -m tapo_scheduler.schedule backup
    python -m tapo_scheduler.schedule clear --yes
    python -m tapo_scheduler.schedule weekly 05:20 on  --days every
    python -m tapo_scheduler.schedule weekly 13:55 off --days every
    python -m tapo_scheduler.schedule once 13:55 off        # one-shot test

Real schedules repeat daily -- the outage plan is the same every day -- so every
rule that matters is `weekly ... --days every`. `once` fires a single time and
never again.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from tapo.requests import DaysOfWeek, ScheduleRule
from tapo.responses import PowerState

from .config import PROJECT_ROOT, Config
from .plug import Plug, connect, format_rule

BACKUP_DIR = PROJECT_ROOT / "schedule-backups"

DAY_SETS = {
    "every": DaysOfWeek.EVERY_DAY,
    "weekdays": DaysOfWeek.WEEKDAYS,
    "weekend": DaysOfWeek.WEEKEND,
}


def parse_state(value: str) -> PowerState:
    if value.lower() in ("on", "1", "true"):
        return PowerState.On
    if value.lower() in ("off", "0", "false"):
        return PowerState.Off
    raise argparse.ArgumentTypeError(f"expected on/off, got {value!r}")


def state_name(state: PowerState) -> str:
    return "ON" if "On" in str(state) else "OFF"


def parse_clock(value: str) -> tuple[int, int]:
    try:
        hour, _, minute = value.partition(":")
        hour, minute = int(hour), int(minute)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected HH:MM, got {value!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise argparse.ArgumentTypeError(f"time out of range: {value!r}")
    return hour, minute


async def target_plugs(config: Config, ips: list[str]) -> list[Plug]:
    plugs = []
    for ip in ips:
        plug = await connect(config, ip)
        await plug.info()
        plugs.append(plug)
    return plugs


def describe(plug: Plug, rules: list, maximum: int) -> None:
    print(f"{plug.ip}  {plug.device_model}  ({len(rules)}/{maximum} rules)")
    for rule in rules:
        print(f"    {format_rule(rule)}")


async def cmd_list(config: Config, ips: list[str]) -> int:
    for plug in await target_plugs(config, ips):
        rules = await plug.schedule_rules()
        describe(plug, rules, await plug.max_schedule_rules())
    return 0


async def cmd_backup(config: Config, ips: list[str]) -> int:
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for plug in await target_plugs(config, ips):
        rules = await plug.schedule_rules()
        payload = {
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "ip": plug.ip,
            "model": plug.device_model,
            "rules": [rule.to_dict() for rule in rules],
        }
        path = BACKUP_DIR / f"{stamp}-{plug.ip}.json"
        path.write_text(json.dumps(payload, indent=2, default=str))
        print(f"{plug.ip}: {len(rules)} rules -> {path.relative_to(PROJECT_ROOT)}")
    return 0


async def cmd_clear(config: Config, ips: list[str], confirm: bool) -> int:
    plugs = await target_plugs(config, ips)
    if not confirm:
        print("Refusing to wipe schedules without --yes. Current state:", file=sys.stderr)
        for plug in plugs:
            rules = await plug.schedule_rules()
            describe(plug, rules, await plug.max_schedule_rules())
        return 2

    for plug in plugs:
        before = len(await plug.schedule_rules())
        await plug.clear_schedule()
        after = len(await plug.schedule_rules())
        print(f"{plug.ip}: {before} -> {after} rules")
    return 0


async def cmd_once(
    config: Config, ips: list[str], at: tuple[int, int], state: PowerState
) -> int:
    hour, minute = at
    rule = ScheduleRule.clock_once(hour, minute, state)
    # ScheduleRule exposes only its factory methods -- no readable attributes --
    # so the confirmation is built from the inputs, and the state is re-read
    # from the device afterwards.
    request = f"once at {hour:02d}:{minute:02d} -> {state_name(state)}"
    for plug in await target_plugs(config, ips):
        await plug.add_schedule_rule(rule)
        print(f"{plug.ip}: added {request}")
        describe(plug, await plug.schedule_rules(), await plug.max_schedule_rules())
    return 0


async def cmd_weekly(
    config: Config,
    ips: list[str],
    at: tuple[int, int],
    state: PowerState,
    days: str,
) -> int:
    hour, minute = at
    rule = ScheduleRule.clock_weekly(hour, minute, DAY_SETS[days], state)
    request = f"weekly {days} at {hour:02d}:{minute:02d} -> {state_name(state)}"
    for plug in await target_plugs(config, ips):
        await plug.add_schedule_rule(rule)
        print(f"{plug.ip}: added {request}")
        describe(plug, await plug.schedule_rules(), await plug.max_schedule_rules())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip", action="append", dest="ips", help="override plug IPs")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show the rules stored on each plug")
    sub.add_parser("backup", help="dump current rules to schedule-backups/")

    clear = sub.add_parser("clear", help="remove every rule from each plug")
    clear.add_argument("--yes", action="store_true", help="required to actually wipe")

    once = sub.add_parser("once", help="add a rule that fires once")
    once.add_argument("time", type=parse_clock, help="HH:MM in the device's timezone")
    once.add_argument("state", type=parse_state, help="on or off")

    weekly = sub.add_parser("weekly", help="add a rule that repeats weekly")
    weekly.add_argument("time", type=parse_clock, help="HH:MM in the device's timezone")
    weekly.add_argument("state", type=parse_state, help="on or off")
    weekly.add_argument("--days", choices=sorted(DAY_SETS), default="every")

    return parser


async def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = Config.load()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    ips = args.ips or config.known_plugs
    if not ips:
        print("error: no plug IPs given (--ip or TAPO_PLUGS)", file=sys.stderr)
        return 2

    if args.command == "list":
        return await cmd_list(config, ips)
    if args.command == "backup":
        return await cmd_backup(config, ips)
    if args.command == "clear":
        return await cmd_clear(config, ips, args.yes)
    if args.command == "once":
        return await cmd_once(config, ips, args.time, args.state)
    if args.command == "weekly":
        return await cmd_weekly(config, ips, args.time, args.state, args.days)
    raise AssertionError(f"unhandled command {args.command}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
