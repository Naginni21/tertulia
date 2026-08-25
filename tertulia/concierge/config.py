"""Concierge configuration (YAML file + a couple of environment overrides).

Secrets never live in the YAML: the bot token is read from the environment
variable named by ``telegram.bot_token_env`` (default ``TERTULIA_BOT_TOKEN``).
``TERTULIA_CHAT_ID`` overrides ``telegram.chat_id`` so example configs can be
committed without real IDs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when the configuration file is missing something essential."""


@dataclass
class TelegramConfig:
    chat_id: int
    bot_token_env: str = "TERTULIA_BOT_TOKEN"
    # Telegram user IDs allowed to run admin commands (e.g. /welcome).
    admin_user_ids: list[int] = field(default_factory=list)

    def bot_token(self) -> str:
        token = os.environ.get(self.bot_token_env, "").strip()
        if not token:
            raise ConfigError(
                f"Telegram bot token not found: set the {self.bot_token_env} environment variable"
            )
        return token


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8741
    # Max seconds a delegate's inbox long-poll is held open.
    long_poll_seconds: int = 25


@dataclass
class RoomConfig:
    name: str = "Tertulia"
    # Language the delegates speak in the room and the concierge uses for its
    # own notes ("es" or "en").
    language: str = "es"
    # Directory with the ritual YAML files (resolved relative to the config file).
    rituals_dir: Path = Path("rituals/es")
    # Joins within this window are welcomed in a single welcome ritual.
    join_grace_seconds: int = 20
    # A delegate is "online" if it polled its inbox within this window.
    online_window_seconds: int = 90


@dataclass
class LimitsConfig:
    # Spontaneous (non-ritual) messages a delegate may post in any 24h window.
    spontaneous_per_24h: int = 3
    # Minimum gap between two spontaneous messages of the same delegate.
    min_gap_seconds: int = 30
    # After this many consecutive spontaneous delegate messages with no human
    # in between, delegates must wait for a human to speak (anti ping-pong).
    max_consecutive_delegate_messages: int = 4
    max_message_chars: int = 2000


@dataclass
class ConciergeConfig:
    telegram: TelegramConfig
    server: ServerConfig
    room: RoomConfig
    limits: LimitsConfig
    db_path: Path
    base_dir: Path


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"'{key}' must be a mapping")
    return value


def load_config(path: str | Path) -> ConciergeConfig:
    path = Path(path).expanduser().resolve()
    base_dir = path.parent
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")

    tg_raw = _section(raw, "telegram")
    chat_id_env = os.environ.get("TERTULIA_CHAT_ID", "").strip()
    chat_id = int(chat_id_env) if chat_id_env else int(tg_raw.get("chat_id") or 0)
    if not chat_id:
        raise ConfigError(
            "telegram.chat_id is required (the Telegram group ID of the room; "
            "run `tertulia-concierge whoami` to discover it, or set TERTULIA_CHAT_ID)"
        )
    admin_raw = tg_raw.get("admin_user_ids") or []
    telegram = TelegramConfig(
        chat_id=chat_id,
        bot_token_env=str(tg_raw.get("bot_token_env", "TERTULIA_BOT_TOKEN")),
        admin_user_ids=[int(x) for x in admin_raw],
    )

    server = ServerConfig(**_section(raw, "server"))

    room_raw = dict(_section(raw, "room"))
    rituals_dir = Path(room_raw.pop("rituals_dir", "rituals/es"))
    room = RoomConfig(rituals_dir=(base_dir / rituals_dir).resolve(), **room_raw)
    if room.language not in ("es", "en"):
        raise ConfigError("room.language must be 'es' or 'en'")

    limits = LimitsConfig(**_section(raw, "limits"))

    db_path = (base_dir / Path(raw.get("db_path", "data/concierge.sqlite"))).resolve()

    return ConciergeConfig(
        telegram=telegram,
        server=server,
        room=room,
        limits=limits,
        db_path=db_path,
        base_dir=base_dir,
    )
