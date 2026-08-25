from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from tertulia.concierge.config import ConciergeConfig, LimitsConfig, RoomConfig, ServerConfig, TelegramConfig

RITUALS_ES = Path(__file__).resolve().parents[1] / "rituals" / "es"


class FakeTelegram:
    """Records sendMessage calls; getUpdates returns whatever the test queued."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self._updates: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._next_id = 1000

    def get_me(self) -> dict[str, Any]:
        return {"username": "fake_bot", "id": 1}

    def send_message(self, chat_id: int, text: str, *, parse_mode: str | None = "HTML", disable_notification: bool = False) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            record = {"chat_id": chat_id, "text": text, "message_id": self._next_id}
            self.sent.append(record)
            return {"message_id": self._next_id}

    def get_updates(self, offset: int | None, *, timeout: int = 25) -> list[dict[str, Any]]:
        with self._lock:
            updates, self._updates = self._updates, []
        return updates

    def queue_human(self, update_id: int, chat_id: int, user_id: int, name: str, text: str, at: float) -> None:
        with self._lock:
            self._updates.append({
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "date": int(at),
                    "chat": {"id": chat_id, "type": "group", "title": "Test room"},
                    "from": {"id": user_id, "is_bot": False, "first_name": name},
                    "text": text,
                },
            })

    @property
    def texts(self) -> list[str]:
        return [s["text"] for s in self.sent]


def make_config(tmp_path: Path, **overrides: Any) -> ConciergeConfig:
    room = RoomConfig(name="Test room", language="es", rituals_dir=RITUALS_ES, join_grace_seconds=1, online_window_seconds=90)
    limits = LimitsConfig(spontaneous_per_24h=3, min_gap_seconds=0, max_consecutive_delegate_messages=4, max_message_chars=2000)
    cfg = ConciergeConfig(
        telegram=TelegramConfig(chat_id=-100123, admin_user_ids=[42]),
        server=ServerConfig(host="127.0.0.1", port=0, long_poll_seconds=5),
        room=room,
        limits=limits,
        db_path=tmp_path / "concierge.sqlite",
        base_dir=tmp_path,
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


@pytest.fixture
def fake_tg() -> FakeTelegram:
    return FakeTelegram()
