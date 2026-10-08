"""Connect to known plugs and dump what they report.

Run with:  python -m tapo_scheduler.probe 192.168.1.50 192.168.1.51
           python -m tapo_scheduler.probe          # uses TAPO_PLUGS from .env
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .config import Config
from .plug import Plug, format_rule

# Tried in order until one authenticates. P110 first because that is the more
# capable handler; P100/P105 covers the rest.
MODEL_CANDIDATES = ("p110", "p100")


async def probe_one(config: Config, ip: str, model: str | None) -> bool:
    models = (model,) if model else MODEL_CANDIDATES
    last_error: Exception | None = None

    for candidate in models:
        try:
            plug = await Plug.connect(config, ip, candidate)
        except Exception as exc:  # noqa: BLE001 - report, then try next model
            last_error = exc
            continue

        info = await plug.info()
        print(f"OK  {info}")
        print(f"    address      : {info.ip}")
        print(f"    handler      : {plug.handler_model}  (device reports {plug.device_model})")
        print(f"    energy meter : {plug.supports_energy}")
        print(f"    raw keys     : {sorted(info.raw)}")

        try:
            maximum = await plug.max_schedule_rules()
            rules = await plug.schedule_rules()
            print(f"    schedule     : {len(rules)}/{maximum} rules  ('!' = disabled)")
            for rule in rules:
                print(f"      {format_rule(rule)}")
        except Exception as exc:  # noqa: BLE001 - schedule is optional
            print(f"    schedule     : unavailable ({exc!r})")

        if plug.supports_energy:
            try:
                print(f"    energy       : {await plug.energy_usage()}")
            except Exception as exc:  # noqa: BLE001 - energy is optional
                print(f"    energy       : unavailable ({exc!r})")

        return True

    print(f"FAIL {ip}: {last_error!r}")
    return False


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ips", nargs="*", help="plug IPs; defaults to TAPO_PLUGS")
    parser.add_argument("--model", choices=MODEL_CANDIDATES, help="skip model autodetect")
    args = parser.parse_args(argv)

    try:
        config = Config.load()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    ips = args.ips or config.known_plugs
    if not ips:
        print("error: no plug IPs given (argv or TAPO_PLUGS in .env)", file=sys.stderr)
        return 2

    results = [await probe_one(config, ip, args.model) for ip in ips]
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
