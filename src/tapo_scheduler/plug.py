"""Thin wrapper around a single Tapo plug.

Both plug families share the pieces we care about -- on/off, timer, and
on-device schedule rules -- so `Plug` normalises over them and only exposes
energy monitoring when the hardware actually reports it.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any

from tapo import ApiClient
from tapo.requests import DaysOfWeek, ScheduleTime

from . import clock

# Models whose handler reports power and energy. Everything else is on/off only.
ENERGY_MODELS = {"p110", "p110m", "p115"}

# Handler families to try when the caller does not know the model. P110 first
# because it is the more capable handler; P100/P105 covers the rest. Both
# families share the on/off and schedule extensions we use.
MODEL_CANDIDATES = ("p110", "p100")

# Bit 0 is Sunday through bit 6, Saturday. Index matches `DaysOfWeek.bits()`.
DAY_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


def decode_nickname(value: str) -> str:
    """Tapo stores nicknames base64-encoded; decode when that is what we got.

    Anything that is not valid base64 (an already-plain name, for instance) is
    returned unchanged.
    """
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return value
    # Reject decodes that produced control characters -- that means the input
    # merely happened to look like base64.
    return decoded if decoded.isprintable() else value


def format_days(days: DaysOfWeek | None) -> str:
    if days is None or days.is_empty():
        return "once"
    bits = days.bits()
    if bits == 0b1111111:
        return "every day"
    if bits == 0b0111110:
        return "weekdays"
    if bits == 0b1000001:
        return "weekend"
    return ",".join(name for bit, name in enumerate(DAY_NAMES) if bits >> bit & 1)


def clock_warning(raw: dict[str, Any]) -> str | None:
    """Complain when a plug keeps a different timezone than the schedule's.

    The rules written to a plug are wall-clock times, and the device fires them
    on its own clock. A plug set to another region therefore shifts every rule
    by the difference -- and nothing else in the chain would look wrong, since
    the calendar, the diff and the post all agree with each other. This is the
    one failure the rest of the program cannot see, so it is worth saying out
    loud.

    Returns a message, or None when the plug is on Kyiv time (or says nothing
    about its region, which is not something to guess about).
    """
    region = raw.get("region")
    if not region or region == clock.KYIV.key:
        return None
    offset = raw.get("time_diff")
    # `time_diff` is minutes east of UTC. It is reported as the standard offset
    # on these devices and does not follow summer time, so it is context for
    # the warning rather than something to check against.
    detail = f" (UTC{offset // 60:+d})" if isinstance(offset, int) else ""
    return (
        f"plug timezone is {region}{detail}, not {clock.KYIV.key} -- its rules "
        f"fire on its own clock, so every time written will land shifted"
    )


def rule_key(rule: Any) -> tuple[int, bool, bool, int]:
    """Canonical form of a *device* rule, for diffing against desired ones.

    ``(minute of day, on, enabled, day bits)``. Sunrise/sunset rules have no
    minute, so they get -1 and therefore never compare equal to a clock rule --
    which is what we want, since they would otherwise linger on the plug.
    """
    time = rule.time
    if isinstance(time, ScheduleTime.Clock):
        minute = time.hour * 60 + time.minute
    else:
        minute = -1
    return (
        minute,
        "On" in str(rule.desired_state),
        bool(rule.enabled),
        rule.days.bits() if rule.days is not None else 0,
    )


def format_rule(rule: Any) -> str:
    """Render a `ScheduleRuleResult` as one line."""
    time = rule.time
    if isinstance(time, ScheduleTime.Clock):
        when = f"{time.hour:02d}:{time.minute:02d}"
    elif isinstance(time, ScheduleTime.Sunrise):
        when = f"sunrise{time.offset_minutes:+d}m"
    elif isinstance(time, ScheduleTime.Sunset):
        when = f"sunset{time.offset_minutes:+d}m"
    else:
        when = f"?{time!r}"

    state = "ON" if "On" in str(rule.desired_state) else "OFF"
    flag = " " if rule.enabled else "!"
    return f"{flag}{when:<10} {format_days(rule.days):<12} -> {state:<3} id={rule.id}"


@dataclass
class PlugInfo:
    model: str
    nickname: str
    ip: str
    device_on: bool
    rssi: int | None
    raw: dict[str, Any]

    def __str__(self) -> str:
        state = "on" if self.device_on else "off"
        signal = f", rssi {self.rssi}" if self.rssi is not None else ""
        return f"{self.nickname} ({self.model}) @ {self.ip} — {state}{signal}"


@dataclass
class EnergyReading:
    """Normalised energy reading. `None` means the device did not report it."""

    watts: float | None = None
    today_wh: int | None = None
    month_wh: int | None = None
    today_runtime_min: int | None = None
    source: str = ""

    def __str__(self) -> str:
        parts = []
        if self.watts is not None:
            parts.append(f"{self.watts:.1f} W")
        if self.today_wh is not None:
            parts.append(f"today {self.today_wh / 1000:.3f} kWh")
        if self.month_wh is not None:
            parts.append(f"month {self.month_wh / 1000:.3f} kWh")
        if self.today_runtime_min is not None:
            parts.append(f"runtime {self.today_runtime_min} min")
        parts.append(f"via {self.source}")
        return "  ".join(parts)


class Plug:
    """One plug at a known IP address."""

    def __init__(self, client: ApiClient, ip: str, model: str):
        self.client = client
        self.ip = ip
        # `model` is the handler family we authenticated with, not the hardware
        # model -- both live under `p110` if that is what connected first.
        self.handler_model = model.lower()
        self.device_model = ""
        self._handler = None
        self._info: PlugInfo | None = None

    @classmethod
    async def connect(cls, config, ip: str, model: str) -> Plug:
        """Authenticate against the plug and return a ready handle."""
        plug = cls(ApiClient(config.email, config.password), ip, model)
        await plug.info()
        return plug

    async def _ensure_handler(self):
        if self._handler is None:
            factory = getattr(self.client, self.handler_model, None)
            if factory is None:
                raise ValueError(f"unsupported plug model: {self.handler_model}")
            self._handler = await factory(self.ip)
        return self._handler

    @property
    def supports_energy(self) -> bool:
        """True when the hardware reports power/energy, not just on/off."""
        return self.device_model.lower() in ENERGY_MODELS

    async def info(self, refresh: bool = False) -> PlugInfo:
        if self._info is not None and not refresh:
            return self._info
        handler = await self._ensure_handler()
        # get_device_info_json is the documented fallback for when the typed
        # deserialization drops fields we care about.
        raw = await handler.get_device_info_json()
        self.device_model = raw.get("model", "") or self.handler_model
        self._info = PlugInfo(
            model=self.device_model,
            nickname=decode_nickname(raw.get("nickname", "") or "?"),
            ip=self.ip,
            device_on=bool(raw.get("device_on", False)),
            rssi=raw.get("rssi"),
            raw=raw,
        )
        return self._info

    async def turn_on(self) -> None:
        await (await self._ensure_handler()).on()

    async def turn_off(self) -> None:
        await (await self._ensure_handler()).off()

    async def energy_usage(self) -> EnergyReading:
        """Best-effort energy reading; not every model implements every call.

        Note the mixed units the library uses, which is why this normalises:
        `get_energy_usage` reports `current_power` in milliwatts, while
        `get_current_power` reports it in watts.
        """
        handler = await self._ensure_handler()
        errors = []

        try:
            raw = (await handler.get_energy_usage()).to_dict()
        except Exception as exc:  # noqa: BLE001 - fall through to the next call
            errors.append(f"get_energy_usage: {exc}")
        else:
            power_mw = raw.get("current_power")
            return EnergyReading(
                watts=power_mw / 1000 if power_mw is not None else None,
                today_wh=raw.get("today_energy"),
                month_wh=raw.get("month_energy"),
                today_runtime_min=raw.get("today_runtime"),
                source="get_energy_usage",
            )

        try:
            raw = (await handler.get_current_power()).to_dict()
        except Exception as exc:  # noqa: BLE001 - fall through to the next call
            errors.append(f"get_current_power: {exc}")
        else:
            return EnergyReading(
                watts=raw.get("current_power"), source="get_current_power"
            )

        try:
            raw = (await handler.get_device_usage()).to_dict()
        except Exception as exc:  # noqa: BLE001 - nothing left to try
            errors.append(f"get_device_usage: {exc}")
        else:
            usage = raw.get("time_usage") or {}
            return EnergyReading(
                today_runtime_min=usage.get("today"), source="get_device_usage"
            )

        raise NotImplementedError("; ".join(errors))

    async def schedule_rules(self) -> list[Any]:
        return await (await self._ensure_handler()).get_schedule_rules()

    async def max_schedule_rules(self) -> int:
        return await (await self._ensure_handler()).get_max_schedule_rules()

    async def clear_schedule(self) -> None:
        await (await self._ensure_handler()).remove_all_schedule_rules()

    async def add_schedule_rule(self, rule) -> Any:
        return await (await self._ensure_handler()).add_schedule_rule(rule)


async def connect(config, ip: str, model: str | None = None) -> Plug:
    """Connect to a plug, guessing the handler family when not told."""
    errors = []
    for candidate in (model,) if model else MODEL_CANDIDATES:
        try:
            return await Plug.connect(config, ip, candidate)
        except Exception as exc:  # noqa: BLE001 - try the next family
            errors.append(f"{candidate}: {exc}")
    raise RuntimeError(f"could not connect to {ip} — " + "; ".join(errors))
