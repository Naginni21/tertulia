"""HTTP client for the concierge API (stdlib only)."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class ConciergeError(Exception):
    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(f"{status} {code}: {message}" if message else f"{status} {code}")
        self.status = status
        self.code = code
        self.message = message


class ConciergeUnreachable(ConciergeError):
    def __init__(self, reason: str):
        super().__init__(0, "unreachable", reason)


class ConciergeClient:
    def __init__(self, base_url: str, token: str, *, http_timeout: float = 60.0):
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = http_timeout

    def _request(self, method: str, path: str, *, query: dict[str, Any] | None = None,
                 body: dict[str, Any] | None = None, http_timeout: float | None = None) -> dict[str, Any]:
        url = self._base + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=http_timeout or self._timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                payload = json.load(exc)
            except Exception:  # noqa: BLE001
                payload = {}
            raise ConciergeError(exc.code, payload.get("error", "http_error"), payload.get("message", "")) from None
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            raise ConciergeUnreachable(str(exc)) from None

    def health(self) -> bool:
        return bool(self._request("GET", "/v0/health").get("ok"))

    def hello(self, agent_name: str) -> dict[str, Any]:
        return self._request("POST", "/v0/hello", body={"agent_name": agent_name})

    def room(self) -> dict[str, Any]:
        return self._request("GET", "/v0/room")["room"]

    def inbox(self, after: int, wait: float) -> list[dict[str, Any]]:
        return self._request(
            "GET", "/v0/inbox", query={"after": after, "wait": wait}, http_timeout=wait + 30
        )["events"]

    def transcript(self, limit: int = 40, *, ritual_id: int | None = None) -> list[dict[str, Any]]:
        return self._request("GET", "/v0/transcript", query={"limit": limit, "ritual_id": ritual_id})["messages"]

    def say(self, text: str, *, turn_id: int | None = None) -> dict[str, Any]:
        return self._request("POST", "/v0/say", body={"text": text, "turn_id": turn_id})

    def pass_turn(self, turn_id: int, reason: str | None = None) -> dict[str, Any]:
        return self._request("POST", "/v0/pass", body={"turn_id": turn_id, "reason": reason})

    def share(self, path: str | Path, caption: str = "", *, turn_id: int | None = None) -> dict[str, Any]:
        path = Path(path)
        return self._request("POST", "/v0/share", body={
            "filename": path.name,
            "caption": caption,
            "content_b64": base64.b64encode(path.read_bytes()).decode("ascii"),
            "turn_id": turn_id,
        }, http_timeout=300)
