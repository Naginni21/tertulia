"""Scheduled rituals: due once per occurrence, reminded once, and ``/ritual <id>`` on demand."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tertulia.concierge.app import Concierge
from tertulia.concierge.rituals import RitualSpecError, Schedule, load_rituals, parse_ritual
from tertulia.concierge.store import Store

from conftest import FakeTelegram, make_config

TZ = ZoneInfo("Europe/Madrid")
RITUALS = Path(__file__).resolve().parents[1] / "rituals"


def ts(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=TZ).timestamp()


def test_schedule_parse_and_occurrences():
    s = Schedule.parse("fri 18:00")
    thursday = ts(2026, 9, 3, 12, 0)
    assert s.after(thursday, TZ) == ts(2026, 9, 4, 18, 0)
    assert s.at_or_before(thursday, TZ) == ts(2026, 8, 28, 18, 0)
    friday_evening = ts(2026, 9, 4, 18, 30)
    assert s.at_or_before(friday_evening, TZ) == ts(2026, 9, 4, 18, 0)
    assert s.after(friday_evening, TZ) == ts(2026, 9, 11, 18, 0)
    for bad in ("friday 18:00", "fri 25:00", "fri", ""):
        with pytest.raises(RitualSpecError):
            Schedule.parse(bad)


def test_schedule_trigger_needs_a_schedule():
    with pytest.raises(RitualSpecError):
        parse_ritual({"id": "x", "trigger": "schedule", "open": "o", "close": "c", "rounds": [{"id": "r", "instruction": "i"}]})
    with pytest.raises(RitualSpecError):
        parse_ritual({"id": "x", "trigger": "hourly", "open": "o", "close": "c", "rounds": [{"id": "r", "instruction": "i"}]})


@pytest.mark.parametrize("lang", ["es", "en"])
def test_shipped_scheduled_rituals_parse(lang):
    specs = load_rituals(RITUALS / lang)
    assert specs["weekly"].schedule == Schedule.parse("fri 18:00") and specs["weekly"].remind
    assert specs["snapshot"].schedule == Schedule.parse("tue 18:00") and specs["snapshot"].remind
    assert len(specs["weekly"].rounds) == 3 and len(specs["snapshot"].rounds) == 2


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def _app(tmp_path, fake_tg, clock, *, timezone="Europe/Madrid", seen_at=None):
    cfg = make_config(tmp_path)
    cfg.room.timezone = timezone
    store = Store(cfg.db_path)
    specs = load_rituals(RITUALS / "es")
    app = Concierge(cfg, store, fake_tg, {"welcome": specs["welcome"], "weekly": specs["weekly"]}, clock=clock)
    d, _ = store.create_delegate("Ana")
    store.register_hello(d.id, "Bot", now=seen_at if seen_at is not None else clock.now)
    return app, store, d


def test_weekly_is_reminded_once_and_runs_once(tmp_path, fake_tg: FakeTelegram):
    clock = Clock(ts(2026, 9, 3, 17, 0))  # Thursday 17:00: 25 h before Friday 18:00
    app, store, d = _app(tmp_path, fake_tg, clock)
    app.tick()
    assert fake_tg.texts == []

    clock.now = ts(2026, 9, 3, 18, 30)  # inside the 24 h reminder window
    app.tick()
    app.tick()
    assert len(fake_tg.texts) == 1 and "el viernes a las 18:00" in fake_tg.texts[0]
    assert "ritual_soon" in [e["kind"] for e in store.events_after(d.id, 0)]

    clock.now = ts(2026, 9, 4, 18, 0) + 5
    store.touch(d.id, now=clock.now)  # online at the time
    app.tick()
    assert app._ritual is not None and app._ritual.spec.id == "weekly"
    assert any("Semanal de autoayuda" in t for t in fake_tg.texts)
    assert store.kv_get("ritual_last_run:weekly") == str(ts(2026, 9, 4, 18, 0))

    clock.now += 60
    app.tick()
    assert app._ritual_queue == []  # not queued a second time


def test_nobody_online_waits_within_the_catch_up_window(tmp_path, fake_tg: FakeTelegram):
    clock = Clock(ts(2026, 9, 4, 18, 0) + 5)
    app, store, d = _app(tmp_path, fake_tg, clock, seen_at=ts(2026, 9, 4, 12, 0))  # asleep since noon
    app.tick()
    assert app._ritual is None and store.kv_get("ritual_last_run:weekly") is None

    clock.now = ts(2026, 9, 4, 19, 0)
    store.touch(d.id, now=clock.now)
    app.tick()
    assert app._ritual is not None and app._ritual.spec.id == "weekly"


def test_missed_by_more_than_the_window_is_skipped(tmp_path, fake_tg: FakeTelegram):
    clock = Clock(ts(2026, 9, 5, 10, 0))  # Saturday morning, 16 h late
    app, store, d = _app(tmp_path, fake_tg, clock)
    app.tick()
    assert app._ritual is None and not any("Semanal" in t for t in fake_tg.texts)


def test_no_timezone_means_no_scheduled_rituals(tmp_path, fake_tg: FakeTelegram):
    clock = Clock(ts(2026, 9, 4, 18, 0) + 5)
    app, store, d = _app(tmp_path, fake_tg, clock, timezone=None)
    app.tick()
    assert app._ritual is None and fake_tg.texts == []


def _cmd(app, text, user_id=42):
    app.handle_update({
        "update_id": 1,
        "message": {"message_id": 1, "date": 0, "chat": {"id": app.cfg.telegram.chat_id, "type": "group"},
                    "from": {"id": user_id, "is_bot": False, "first_name": "F"}, "text": text},
    })


def test_ritual_command(tmp_path, fake_tg: FakeTelegram):
    clock = Clock(ts(2026, 9, 5, 10, 0))
    app, store, d = _app(tmp_path, fake_tg, clock)
    _cmd(app, "/ritual weekly", user_id=7)
    assert "administrador" in fake_tg.texts[-1]
    _cmd(app, "/ritual bingo")
    assert "No conozco ese ritual" in fake_tg.texts[-1] and "weekly" in fake_tg.texts[-1]
    _cmd(app, "/ritual weekly")
    app.tick()
    assert app._ritual is not None and app._ritual.spec.id == "weekly"
    assert "Próximo ritual" in app.status_text()
