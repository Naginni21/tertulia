"""Minimal Telegram Bot API client (stdlib only, long polling)."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("tertulia.telegram")


class TelegramError(Exception):
    def __init__(self, method: str, code: int | None, description: str, retry_after: int | None = None):
        super().__init__(f"{method}: [{code}] {description}")
        self.method = method
        self.code = code
        self.description = description
        self.retry_after = retry_after


class TelegramClient:
    """Thin wrapper over ``https://api.telegram.org/bot<token>/<method>``.

    Only the handful of methods the concierge needs. The token never appears
    in logs or exceptions.
    """

    def __init__(self, token: str, *, base_url: str = "https://api.telegram.org", http_timeout: float = 40.0):
        self._url = f"{base_url}/bot{token}"
        self._timeout = http_timeout

    def call(self, method: str, *, http_timeout: float | None = None, **params: Any) -> Any:
        body = json.dumps({k: v for k, v in params.items() if v is not None}).encode("utf-8")
        req = urllib.request.Request(
            f"{self._url}/{method}", data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=http_timeout or self._timeout) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                data = json.load(exc)
            except Exception:  # noqa: BLE001 - body is not JSON, use the HTTP status
                raise TelegramError(method, exc.code, exc.reason) from None
            params_ = data.get("parameters") or {}
            raise TelegramError(
                method, data.get("error_code", exc.code), data.get("description", exc.reason),
                retry_after=params_.get("retry_after"),
            ) from None
        if not data.get("ok"):
            raise TelegramError(method, data.get("error_code"), data.get("description", "unknown error"))
        return data["result"]

    # --- the methods we use ---------------------------------------------------

    def get_me(self) -> dict[str, Any]:
        return self.call("getMe")

    def get_updates(self, offset: int | None, *, timeout: int = 25) -> list[dict[str, Any]]:
        # The HTTP timeout must exceed Telegram's long-poll timeout.
        return self.call(
            "getUpdates",
            http_timeout=timeout + 10,
            offset=offset,
            timeout=timeout,
            allowed_updates=["message"],
        )

    def send_message(
        self, chat_id: int, text: str, *, parse_mode: str | None = "HTML", disable_notification: bool = False
    ) -> dict[str, Any]:
        return self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            disable_notification=disable_notification,
            link_preview_options={"is_disabled": True},
        )


def html_escape(text: str) -> str:
    """Escape text for Telegram's HTML parse mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
