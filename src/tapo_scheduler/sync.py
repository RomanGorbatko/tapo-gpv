"""Push the day's outage schedule onto the plugs.

Reads the operator's channel, takes today's ГПВ schedule for each plug's
sub-queue, turns it into ON/OFF transitions, and writes them to the device as
weekly-every-day rules so they keep firing with the Mac, the router and the
Tapo cloud all offline.

    python -m tapo_scheduler.sync --dry-run     # show what would change
    python -m tapo_scheduler.sync               # apply once
    python -m tapo_scheduler.sync --watch       # poll, re-apply on change

A new schedule for the day, a revision of it, or the rollover to a day whose
schedule was already published all end up as the same thing: the desired rule
set no longer matches what is on the plug, so the plug is wiped and reloaded
from scratch.

The diff is not just an optimisation. Reinstalling unconditionally every minute
would rewrite the device's config flash 1440 times a day, so the poll compares
first and only writes when the rules actually differ.

Two failure modes are deliberately distinct:

* **No schedule for today** -- refuse and change nothing. Absence is ambiguous
  (the operator may have posted only an infographic, or may genuinely have no
  outages planned), and guessing "no outages" would leave the boiler running
  through a real blackout. Pass ``--assume-no-outages`` to say so explicitly.
* **A queue missing from today's post** -- refuse. That means the address is on
  a differently-numbered sub-queue than the config claims.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime
from pathlib import Path

from tapo.requests import DaysOfWeek, ScheduleRule
from tapo.responses import PowerState

from .config import PROJECT_ROOT, Config, PlugTarget
from .notify import (
    Notifier,
    fingerprint,
    missing_schedule_message,
    rozetky,
    schedule_message,
)
from .outage import (
    OFF_LEAD_MINUTES,
    ON_LAG_MINUTES,
    Calendar,
    OutageSchedule,
    Rule,
    format_date_uk,
    load_calendar,
)
from .plug import Plug, connect, format_rule, rule_key

# Where the notifier remembers what it last announced, so a restart does not
# re-post the schedule that is already in the channel.
NOTIFY_STATE = PROJECT_ROOT / ".state" / "notify.json"

EVERY_DAY_BITS = DaysOfWeek.EVERY_DAY.bits()


class PassLog:
    """A pass's routine chatter, held back until the pass turns out to matter.

    In ``--watch`` the pass runs every minute, and the calendar line plus one
    line per plug are identical 1440 times a day. Printing them buries the one
    pass that actually had news, and turns the log into something nobody reads.

    So the routine lines are buffered and released only when the pass changed
    something. Warnings are never held back: they go straight to stderr, where
    they are visible whether or not anything else happened.
    """

    def __init__(self, *, always: bool = False):
        # `always` is for one-off runs, where the user is watching and silence
        # would look like the command had not run at all.
        self.always = always
        self._lines: list[str] = []

    def say(self, line: str) -> None:
        if self.always:
            print(line)
        else:
            self._lines.append(line)

    def flush(self) -> None:
        for line in self._lines:
            print(line)
        self._lines.clear()


def group_by_queue(targets: list[PlugTarget]) -> dict[str, list[PlugTarget]]:
    """Plugs sharing a sub-queue get one message and one rule computation.

    Order is preserved so the log reads in the order the plugs are configured.
    """
    grouped: dict[str, list[PlugTarget]] = {}
    for target in targets:
        assert target.queue is not None  # checked by `select_targets`
        grouped.setdefault(target.queue, []).append(target)
    return grouped


def to_device_rules(actions: tuple[Rule, ...]) -> list[ScheduleRule]:
    """Turn the mirror transitions into rules the plug repeats every day."""
    return [
        ScheduleRule.clock_weekly(
            rule.minute // 60,
            rule.minute % 60,
            DaysOfWeek.EVERY_DAY,
            PowerState.On if rule.on else PowerState.Off,
        )
        for rule in actions
    ]


def desired_keys(actions: tuple[Rule, ...]) -> list[tuple[int, bool, bool, int]]:
    return sorted((rule.minute, rule.on, True, EVERY_DAY_BITS) for rule in actions)


async def apply_to_plug(
    plug: Plug, actions: tuple[Rule, ...], *, dry_run: bool, log: PassLog | None = None
) -> bool:
    """Make the plug's rules match ``actions``. Returns True when it changed."""
    if log is None:
        log = PassLog(always=True)
    current = await plug.schedule_rules()
    have = sorted(rule_key(rule) for rule in current)
    want = desired_keys(actions)
    if have == want:
        return False

    log.say(f"    was  ({len(have)} rules)")
    for rule in current:
        log.say(f"        {format_rule(rule)}")
    log.say(f"    now  ({len(want)} rules)")
    for rule in actions:
        log.say(f"        {rule}")

    if dry_run:
        log.say("    -- dry run, nothing written")
        return True

    await plug.clear_schedule()
    for rule in to_device_rules(actions):
        await plug.add_schedule_rule(rule)

    after = sorted(rule_key(rule) for rule in await plug.schedule_rules())
    if after != want:
        raise RuntimeError(
            f"{plug.ip}: device did not accept the rules "
            f"(wanted {len(want)}, read back {len(after)})"
        )
    return True


async def sync_once(
    config: Config,
    *,
    channel: str,
    posts: int,
    on: date | None,
    lead: int,
    lag: int,
    dry_run: bool,
    assume_no_outages: bool,
    targets: list[PlugTarget],
    notifier: Notifier,
    verbose: bool,
) -> tuple[int, bool]:
    """One pass. Returns ``(exit code, whether anything was written)``."""
    today = on or datetime.now().date()
    calendar: Calendar = await asyncio.to_thread(
        load_calendar, channel, posts=posts, today=on
    )
    log = PassLog(always=verbose)
    log.say(f"calendar : {calendar}")

    schedule: OutageSchedule | None = calendar.today
    if schedule is None:
        if not assume_no_outages:
            print(
                "error: no ГПВ schedule for today -- plugs left untouched. "
                "If the operator really planned no outages, re-run with "
                "--assume-no-outages.",
                file=sys.stderr,
            )
            notifier.post(
                f"alarm:{today}",
                f"missing:{today}",
                missing_schedule_message(today, kept=True),
            )
            return 2, False
        log.say("note: no schedule for today, treating as no outages")

    changed = False
    for queue, group in group_by_queue(targets).items():
        if schedule is None:
            actions: tuple[Rule, ...] = ()
        else:
            try:
                actions = schedule.mirror_rules(queue, lead=lead, lag=lag)
            except KeyError as exc:
                print(f"error: {group[0].ip}: {exc}", file=sys.stderr)
                log.flush()  # the error is the news, but the context still helps
                return 2, changed

        queue_changed = False
        for target in group:
            log.say(f"{target.ip}  queue {queue}  ({len(actions)} rules)")
            plug = await connect(config, target.ip)
            if await apply_to_plug(plug, actions, dry_run=dry_run, log=log):
                queue_changed = True
            else:
                log.say("    unchanged")

        changed = changed or queue_changed
        # Released before the post, so the log reads in the order things happened.
        if queue_changed:
            log.flush()

        # One message per queue. The notifier remembers what it last announced
        # for this queue *and this date*, so a quiet poll is silent, a revision
        # that moves the rules is not, and a restarted watcher does not re-post.
        if schedule is None:
            plugs = f"{len(group)} tapo-{rozetky(len(group))}"
            notifier.post(
                f"queue:{queue}:{today}",
                "no-outages",
                f"📅 Графік на {format_date_uk(today)} · черга {queue}\n\n"
                f"✅ Відключень за ГПВ немає.\n\n🔌 {plugs} — правила знято.",
            )
        else:
            notifier.post(
                f"queue:{queue}:{schedule.date}",
                fingerprint(actions),
                schedule_message(schedule, queue, actions, plug_count=len(group)),
            )

    return 0, changed


def select_targets(config: Config, ips: list[str] | None) -> list[PlugTarget] | None:
    """Resolve which plugs to touch, refusing if any lacks a queue."""
    targets = config.targets
    if ips:
        wanted = set(ips)
        targets = [target for target in targets if target.ip in wanted]
        missing = wanted - {target.ip for target in targets}
        if missing:
            print(f"error: not in TAPO_PLUGS: {', '.join(sorted(missing))}", file=sys.stderr)
            return None

    if not targets:
        print("error: no plugs configured (TAPO_PLUGS)", file=sys.stderr)
        return None

    without = [target.ip for target in targets if not target.queue]
    if without:
        print(
            "error: no ГПВ sub-queue for " + ", ".join(without) + ". "
            "TAPO_PLUGS entries look like 192.168.68.62:3.1 -- the queue comes from "
            "https://www.cherkasyoblenergo.com/off or the Cherkasyoblenergo_bot.",
            file=sys.stderr,
        )
        return None
    return targets


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip", action="append", dest="ips", help="only these plugs")
    parser.add_argument("--channel", default="pat_cherkasyoblenergo", help="public channel")
    parser.add_argument("--date", help="pretend today is this date (YYYY-MM-DD)")
    parser.add_argument("--posts", type=int, default=40, help="how far back to read")
    parser.add_argument("--lead", type=int, default=OFF_LEAD_MINUTES, help="minutes early to cut")
    parser.add_argument("--lag", type=int, default=ON_LAG_MINUTES, help="minutes late to restore")
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    parser.add_argument(
        "--assume-no-outages",
        action="store_true",
        help="treat a missing schedule as an outage-free day instead of an error",
    )
    parser.add_argument("--watch", action="store_true", help="keep polling")
    parser.add_argument("--interval", type=int, default=60, help="seconds between polls")
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="do not post to the channel (rules are still applied)",
    )
    parser.add_argument(
        "--state",
        default=str(NOTIFY_STATE),
        help="where to remember what was last announced",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="print every pass, not only the ones that changed something",
    )
    return parser


async def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = Config.load()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    targets = select_targets(config, args.ips)
    if targets is None:
        return 2

    if not args.no_notify and config.notifier is None:
        print(
            "warning: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set -- "
            "the schedule will be applied but nothing posted. "
            "Use --no-notify to silence this.",
            file=sys.stderr,
        )

    notifier = Notifier(
        config.notifier,
        enabled=not args.no_notify,
        dry_run=args.dry_run,
        state_path=Path(args.state),
    )

    on = date.fromisoformat(args.date) if args.date else None
    options = dict(
        channel=args.channel,
        posts=args.posts,
        on=on,
        lead=args.lead,
        lag=args.lag,
        dry_run=args.dry_run,
        assume_no_outages=args.assume_no_outages,
        targets=targets,
        notifier=notifier,
        # A one-off run is something the user is watching, so it reports what it
        # found. A watcher runs every minute and speaks only when there is news.
        verbose=args.verbose or not args.watch,
    )

    while True:
        try:
            code, changed = await sync_once(config, **options)
        except Exception as exc:  # noqa: BLE001 - a watch loop must survive one bad pass
            print(f"error: {exc}", file=sys.stderr)
            code, changed = 1, False

        if not args.watch:
            # A post that never arrived is worth knowing about, but the rules
            # are on the plug either way.
            return 1 if notifier.failures else code
        # In watch mode a quiet pass stays quiet, so a slow log means "no news".
        if changed or notifier.failures:
            print(f"pass at {datetime.now():%Y-%m-%d %H:%M}  changed={changed}")
        await asyncio.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
