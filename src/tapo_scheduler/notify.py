"""Post updates to a Telegram channel through a bot.

    python -m tapo_scheduler.notify --check          # validate token and chat
    python -m tapo_scheduler.notify "текст"          # send one message

Uses the plain HTTP Bot API over stdlib `urllib` -- no `requests`, no
`python-telegram-bot`. Two calls is the whole surface we need: `getMe` to prove
the token is live and `sendMessage` to post.

The bot must be an **administrator of the channel** with the right to post, or
`sendMessage` fails with "chat not found" / "not enough rights". Being a member
is not enough.

Messages are sent as plain text: no `parse_mode`. Ukrainian schedule text is
full of characters that Markdown and HTML modes both treat as syntax (dashes,
dots in `5.1`, `«»`), and a mis-escaped character makes the API reject the whole
message. Plain text always renders.

The token is a credential: it lives in `.env` (`TELEGRAM_BOT_TOKEN`) and is
never echoed. Anyone holding it can post as the bot.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_API = "https://api.telegram.org"
TIMEOUT_SECONDS = 20


class NotifyError(RuntimeError):
    """The Bot API refused the call, or answered with ok=false."""


class NotifyConfigError(NotifyError):
    """The token or chat id is missing from the environment."""


@dataclass(frozen=True)
class Bot:
    # `repr=False` keeps the token out of tracebacks, logs and error reports --
    # anything holding it can post as the bot.
    token: str = field(repr=False)
    chat_id: str
    api: str = DEFAULT_API

    def _call(self, method: str, payload: dict[str, object]) -> dict:
        url = f"{self.api}/bot{self.token}/{method}"
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                answer = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # The Bot API explains refusals in the body, which is the useful part.
            detail = exc.read().decode("utf-8", "replace")
            raise NotifyError(f"{method} failed: HTTP {exc.code} {detail}") from None
        except urllib.error.URLError as exc:
            raise NotifyError(f"{method} failed: {exc.reason}") from None

        if not answer.get("ok"):
            raise NotifyError(f"{method} refused: {answer.get('description', answer)}")
        return answer

    def send(self, text: str) -> dict:
        """Post one message. Raises `NotifyError`; never silently drops."""
        return self._call(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": True,
                # The channel is about power outages; a silent post that nobody
                # notices is not worth sending.
                "disable_notification": False,
            },
        )

    def describe(self) -> str:
        """What this bot and chat are, for a pre-flight check."""
        me = self._call("getMe", {})["result"]
        chat = self._call("getChat", {"chat_id": self.chat_id})["result"]
        return (
            f"bot  : @{me.get('username')} ({me.get('first_name')}) id={me.get('id')}\n"
            f"chat : {chat.get('title')} type={chat.get('type')} id={chat.get('id')}"
        )


def from_env(env: dict[str, str] | None = None) -> Bot:
    """Build a Bot from ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID``.

    Raises `NotifyConfigError` naming what is missing, rather than posting to
    the wrong place.
    """
    import os

    source = env if env is not None else os.environ
    token = (source.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (source.get("TELEGRAM_CHAT_ID") or "").strip()
    missing = [
        name
        for name, value in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat_id))
        if not value
    ]
    if missing:
        raise NotifyConfigError("missing from .env: " + ", ".join(missing))
    return Bot(token=token, chat_id=chat_id)


def rozetky(count: int) -> str:
    """Ukrainian plural for "розетка": 1 розетка, 2-4 розетки, 5+ розеток.

    The teens are the exception they always are: 11 and 12 take the plural of
    the many (розеток), not of the few.
    """
    if count % 100 in range(11, 15):
        return "розеток"
    last = count % 10
    if last == 1:
        return "розетка"
    if 2 <= last <= 4:
        return "розетки"
    return "розеток"


def schedule_message(schedule, queue: str, actions, *, plug_count: int) -> str:
    """The update posted after the plugs have been reloaded.

    Written for a phone: what people plan around is the announced outage, so
    that is the message. The plug transitions are deliberately not listed --
    they follow from the outages by a fixed rule, and forty lines of them push
    the actual schedule off the screen.
    """
    from .outage import format_date_uk, format_minute

    outages = schedule.windows(queue)
    lines = [f"📅 Графік на {format_date_uk(schedule.date)} · черга {queue}", ""]

    if not outages:
        lines += ["✅ Відключень за ГПВ немає.", ""]
    else:
        lines.append("⛔️ Відключення:")
        lines += [
            f"{format_minute(window.start)}–{format_minute(window.end)}"
            for window in outages
        ]
        lines.append("")

    plugs = f"{plug_count} tapo-{rozetky(plug_count)}"
    if not actions:
        lines.append(f"🔌 {plugs} — правила знято.")
    else:
        lines.append(f"🔌 {plugs} успішно синхронізовано.")

    if schedule.source is not None:
        lines += ["", f"Джерело: {schedule.source.url}"]
    return "\n".join(lines)


def missing_schedule_message(when, *, kept: bool = True) -> str:
    """The alarm. Absence of a schedule is not the same as no outages."""
    from .outage import format_date_uk

    tail = (
        "Розетки лишено без змін — працюють за попереднім графіком."
        if kept
        else "Розетки без графіку."
    )
    return (
        f"⚠️ Немає графіку ГПВ на {format_date_uk(when)}.\n\n"
        f"{tail}\n"
        "Якщо відключень справді не планується — потрібен явний --assume-no-outages."
    )


class Notifier:
    """Posts channel updates, and refuses to repeat itself.

    In `--watch` mode a pass runs every minute. Without a memory of what was
    last announced, an outage-free day would post the same text 1440 times and a
    missing schedule would alarm all night.

    The memory is on disk, not just in the process: a restarted watcher would
    otherwise re-announce the current schedule every time it comes up.

    The fingerprint is the *state*, not the text -- a revision that lands back on
    an earlier set of rules is announced again, because the state changed even
    though the text is one we have sent before.
    """

    def __init__(
        self,
        bot,
        *,
        enabled: bool = True,
        dry_run: bool = False,
        state_path: Path | None = None,
    ):
        self.bot = bot
        self.enabled = enabled and bot is not None
        self.dry_run = dry_run
        self.state_path = state_path
        self.failures = 0
        self._last: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        if self.state_path is None or not self.state_path.exists():
            return {}
        try:
            data = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            # A corrupt state file must not stop the sync; worst case is one
            # duplicate post.
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self._last, ensure_ascii=False, indent=2))
        except OSError as exc:
            print(f"    could not save notify state: {exc}", file=sys.stderr)

    def post(self, key: str, fingerprint: str, text: str) -> bool:
        if self._last.get(key) == fingerprint:
            return False

        # A run that does not actually send must not record anything: otherwise
        # a `--dry-run` rehearsal makes the next real run think the schedule was
        # already announced, and the channel silently stays empty.
        if not self.enabled:
            print("    [notify off] not posting:")
            for line in text.splitlines():
                print(f"      | {line}")
            return False
        if self.dry_run:
            print("    [dry run] would post to the channel:")
            for line in text.splitlines():
                print(f"      | {line}")
            return False

        # Remember before sending: a post that fails the first time should not be
        # retried in a tight loop either.
        self._last[key] = fingerprint
        self._save()

        try:
            answer = self.bot.send(text)
        except NotifyError as exc:
            # The rules are already on the plug; a failed post must not undo that
            # or kill a watch loop. It is reported through the exit code instead.
            print(f"    notify failed: {exc}", file=sys.stderr)
            self.failures += 1
            return False
        print(f"    posted message_id={answer['result'].get('message_id')}")
        return True


def fingerprint(actions) -> str:
    """Stable identity of a rule set, for the notifier's memory."""
    return ",".join(f"{rule.minute}:{'1' if rule.on else '0'}" for rule in actions)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", nargs="?", help="message to send")
    parser.add_argument("--check", action="store_true", help="validate token and chat only")
    return parser


def main(argv: list[str] | None = None) -> int:
    from .config import Config

    args = build_parser().parse_args(argv)
    try:
        Config.load()
        bot = from_env()
    except (RuntimeError, NotifyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.check:
        try:
            print(bot.describe())
        except NotifyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if not args.text:
        print("error: no message given (or use --check)", file=sys.stderr)
        return 2

    try:
        answer = bot.send(args.text)
    except NotifyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"sent message_id={answer['result'].get('message_id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
