"""Tests for the channel notifier.

Two things here are worth pinning down: the message has to render (plain text,
no parse_mode, because Ukrainian schedule text is full of characters the
formatting modes treat as syntax), and the notifier must not repeat itself --
in `--watch` mode it runs every minute, and across restarts.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from tapo_scheduler.notify import (
    Bot,
    Notifier,
    from_env,
    missing_schedule_message,
    rozetky,
    schedule_message,
)
from tapo_scheduler.outage import OutageSchedule, Rule, Window

SCHEDULE = OutageSchedule(
    date=date(2026, 10, 8),
    queues={
        "5.1": (Window(9 * 60, 11 * 60), Window(13 * 60, 15 * 60)),
        "6.2": (Window(0, 60),),
    },
)
ACTIONS = (Rule(8 * 60 + 55, False), Rule(11 * 60 + 20, True))
MORE_ACTIONS = (Rule(12 * 60 + 55, False), Rule(15 * 60 + 20, True))


class FakeBot:
    def __init__(self, fail: bool = False):
        self.chat_id = "-100"
        self.sent: list[str] = []
        self.fail = fail

    def send(self, text: str) -> dict:
        if self.fail:
            from tapo_scheduler.notify import NotifyError

            raise NotifyError("chat not found")
        self.sent.append(text)
        return {"result": {"message_id": len(self.sent)}}


def notifier(bot, tmp_path, **kwargs) -> Notifier:
    return Notifier(bot, state_path=tmp_path / "notify.json", **kwargs)


# --- messages ---------------------------------------------------------------


def test_schedule_message_lists_outages_not_transitions() -> None:
    text = schedule_message(SCHEDULE, "5.1", ACTIONS, plug_count=2)
    assert "8 жовтня" in text
    assert "черга 5.1" in text
    assert "09:00–11:00" in text
    assert "13:00–15:00" in text
    assert "🔌 2 tapo-розетки успішно синхронізовано." in text
    # The plug transitions follow from the outages by a fixed rule; listing them
    # pushes the schedule off the screen of a phone.
    assert "08:55" not in text
    assert "11:20" not in text


def test_schedule_message_renders_a_midnight_window() -> None:
    """`0` is falsy; a careless f-string would print a bare zero here."""
    text = schedule_message(SCHEDULE, "6.2", (), plug_count=1)
    assert "00:00–01:00" in text


def test_schedule_message_handles_no_outages() -> None:
    empty = OutageSchedule(date=date(2026, 10, 8), queues={"5.1": ()})
    text = schedule_message(empty, "5.1", (), plug_count=1)
    assert "Відключень за ГПВ немає" in text
    assert "🔌 1 tapo-розетка — правила знято." in text


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, "1 tapo-розетка"),
        (2, "2 tapo-розетки"),
        (4, "4 tapo-розетки"),
        (5, "5 tapo-розеток"),
        (11, "11 tapo-розеток"),
        (21, "21 tapo-розетка"),
        (22, "22 tapo-розетки"),
    ],
)
def test_rozetky_plural(count: int, expected: str) -> None:
    assert f"{count} tapo-{rozetky(count)}" == expected


def test_missing_schedule_message_says_the_plugs_are_untouched() -> None:
    text = missing_schedule_message(date(2026, 10, 8))
    assert "Немає графіку ГПВ на 8 жовтня" in text
    assert "без змін" in text
    assert "--assume-no-outages" in text


# --- dedupe -----------------------------------------------------------------


def test_the_same_state_is_not_announced_twice(tmp_path) -> None:
    bot = FakeBot()
    n = notifier(bot, tmp_path)
    assert n.post("q", "same", "text") is True
    assert n.post("q", "same", "text") is False
    assert bot.sent == ["text"]


def test_a_changed_state_is_announced_again(tmp_path) -> None:
    bot = FakeBot()
    n = notifier(bot, tmp_path)
    n.post("q", "A", "text a")
    n.post("q", "B", "text b")
    assert bot.sent == ["text a", "text b"]


def test_returning_to_an_earlier_state_is_announced(tmp_path) -> None:
    """The fingerprint is the state, not the text: A -> B -> A must post again."""
    bot = FakeBot()
    n = notifier(bot, tmp_path)
    n.post("q", "A", "text a")
    n.post("q", "B", "text b")
    n.post("q", "A", "text a")
    assert bot.sent == ["text a", "text b", "text a"]


def test_a_restart_does_not_repost(tmp_path) -> None:
    """A restarted watcher must not re-announce the schedule already posted."""
    bot = FakeBot()
    notifier(bot, tmp_path).post("q", "same", "text")
    assert notifier(bot, tmp_path).post("q", "same", "text") is False
    assert bot.sent == ["text"]


def test_a_corrupt_state_file_is_not_fatal(tmp_path) -> None:
    path = tmp_path / "notify.json"
    path.write_text("{not json")
    bot = FakeBot()
    assert Notifier(bot, state_path=path).post("q", "same", "text") is True


def test_state_file_is_keyed_per_queue_and_date(tmp_path) -> None:
    bot = FakeBot()
    n = notifier(bot, tmp_path)
    n.post("queue:5.1:2026-10-08", "A", "today 5.1")
    n.post("queue:6.2:2026-10-08", "A", "today 6.2")
    n.post("queue:5.1:2026-10-09", "A", "tomorrow 5.1")
    assert bot.sent == ["today 5.1", "today 6.2", "tomorrow 5.1"]


# --- failure handling -------------------------------------------------------


def test_a_failed_post_does_not_raise(tmp_path) -> None:
    """The rules are already on the plug; a dead channel must not undo that."""
    n = notifier(FakeBot(fail=True), tmp_path)
    assert n.post("q", "A", "text") is False
    assert n.failures == 1


def test_a_failed_post_is_not_retried_in_a_loop(tmp_path) -> None:
    bot = FakeBot(fail=True)
    n = notifier(bot, tmp_path)
    n.post("q", "A", "text")
    assert n.post("q", "A", "text") is False
    assert n.failures == 1


def test_disabled_notifier_sends_nothing(tmp_path) -> None:
    bot = FakeBot()
    n = notifier(bot, tmp_path, enabled=False)
    assert n.post("q", "A", "text") is False
    assert bot.sent == []


def test_dry_run_does_not_poison_the_state(tmp_path) -> None:
    """A rehearsal must not make the next real run think it already posted."""
    bot = FakeBot()
    path = tmp_path / "notify.json"
    Notifier(bot, dry_run=True, state_path=path).post("q", "A", "text")
    assert not path.exists()
    assert Notifier(bot, state_path=path).post("q", "A", "text") is True
    assert bot.sent == ["text"]


def test_disabled_notifier_does_not_poison_the_state(tmp_path) -> None:
    bot = FakeBot()
    path = tmp_path / "notify.json"
    Notifier(bot, enabled=False, state_path=path).post("q", "A", "text")
    assert not path.exists()


def test_a_missing_bot_disables_posting(tmp_path) -> None:
    n = Notifier(None, state_path=tmp_path / "notify.json")
    assert n.post("q", "A", "text") is False


def test_no_state_path_still_dedupes_within_the_process() -> None:
    bot = FakeBot()
    n = Notifier(bot)
    n.post("q", "A", "text")
    assert n.post("q", "A", "text") is False


# --- credentials ------------------------------------------------------------


def test_from_env_reads_both_values() -> None:
    bot = from_env({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "-100"})
    assert (bot.token, bot.chat_id) == ("t", "-100")


def test_from_env_names_what_is_missing() -> None:
    import pytest

    from tapo_scheduler.notify import NotifyConfigError

    with pytest.raises(NotifyConfigError, match="TELEGRAM_CHAT_ID"):
        from_env({"TELEGRAM_BOT_TOKEN": "t"})


def test_token_is_not_in_the_repr() -> None:
    """A token that leaks into a log or a traceback is a token that leaks."""
    bot = Bot(token="secret-token", chat_id="-100")
    assert "secret-token" not in repr(bot)


def test_state_file_records_fingerprints_not_text(tmp_path) -> None:
    bot = FakeBot()
    n = notifier(bot, tmp_path)
    n.post("q", "A", "some text")
    saved = json.loads((tmp_path / "notify.json").read_text())
    assert saved == {"q": "A"}
