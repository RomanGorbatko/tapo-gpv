"""Credentials and plug inventory, loaded from the environment / .env."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# A ГПВ sub-queue id: "3", "3.1", ... The operator publishes 1.1 through 6.2.
QUEUE_RE = re.compile(r"^\d+(?:\.\d+)?$")


@dataclass(frozen=True)
class PlugTarget:
    """A plug, plus the ГПВ sub-queue the address on it sits on.

    The queue is optional because the discovery and probing tools do not need
    it; only the sync does, and it refuses to guess.
    """

    ip: str
    queue: str | None = None

    def __str__(self) -> str:
        return f"{self.ip}:{self.queue}" if self.queue else self.ip

    def to_dict(self) -> dict[str, str | None]:
        return {"ip": self.ip, "queue": self.queue}


def parse_target(raw: str) -> PlugTarget:
    """Parse ``192.168.68.62:3.1`` into address plus queue.

    Splits on the last colon, and only reads the tail as a queue when the head
    has no colon left in it -- which is what tells ``fe80::1`` (an IPv6 literal)
    apart from ``plug:3.1``. IPv6 addresses therefore cannot carry a queue: the
    two readings are genuinely indistinguishable, and guessing wrong would point
    a plug at somebody else's outage hours.
    """
    raw = raw.strip()
    head, separator, tail = raw.rpartition(":")
    if separator and ":" not in head:
        address, queue = head.strip(), tail.strip()
        if QUEUE_RE.match(queue):
            return PlugTarget(ip=address, queue=queue)
    return PlugTarget(ip=raw)


@dataclass(frozen=True)
class Config:
    email: str
    password: str
    targets: list[PlugTarget] = field(default_factory=list)
    # Optional: without these the sync still runs, it just cannot post updates.
    bot_token: str | None = None
    chat_id: str | None = None

    @property
    def known_plugs(self) -> list[str]:
        """Just the addresses, for the tools that do not care about queues."""
        return [target.ip for target in self.targets]

    def queue_for(self, ip: str) -> str | None:
        for target in self.targets:
            if target.ip == ip:
                return target.queue
        return None

    @property
    def notifier(self):
        """A `notify.Bot`, or None when the channel is not configured."""
        from .notify import Bot

        if not self.bot_token or not self.chat_id:
            return None
        return Bot(token=self.bot_token, chat_id=self.chat_id)

    @classmethod
    def load(cls, env_file: Path | None = None) -> Config:
        load_dotenv(env_file or PROJECT_ROOT / ".env")
        email = os.getenv("TAPO_EMAIL", "").strip()
        password = os.getenv("TAPO_PASSWORD", "").strip()
        if not email or not password:
            raise RuntimeError(
                "TAPO_EMAIL / TAPO_PASSWORD missing. "
                f"Copy .env.example to {PROJECT_ROOT / '.env'} and fill them in."
            )
        targets = [
            parse_target(chunk)
            for chunk in os.getenv("TAPO_PLUGS", "").split(",")
            if chunk.strip()
        ]
        return cls(
            email=email,
            password=password,
            targets=targets,
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None,
            chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip() or None,
        )
