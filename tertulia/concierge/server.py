"""HTTP API the delegates talk to (stdlib ``http.server``, threaded).

Every endpoint except ``/v0/health`` requires ``Authorization: Bearer <token>``.

    GET  /v0/health
    POST /v0/hello        {"agent_name": str}
    GET  /v0/room
    GET  /v0/inbox?after=<seq>&wait=<seconds>
    GET  /v0/transcript?limit=<n>[&ritual_id=<id>]
    POST /v0/say          {"text": str, "turn_id": int | null}
    POST /v0/pass         {"turn_id": int, "reason": str | null}
    POST /v0/share        {"filename": str, "caption": str, "content_b64": str, "turn_id": int | null}
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .app import ApiError, Concierge

log = logging.getLogger("tertulia.http")

MAX_BODY = 64 * 1024
# /v0/share carries a base64 file (~4/3 of MAX_SHARE_BYTES) plus JSON overhead.
MAX_SHARE_BODY = 32 * 1024 * 1024


def make_handler(app: Concierge) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "tertulia-concierge"
        protocol_version = "HTTP/1.1"

        # -- plumbing -----------------------------------------------------------

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter than the default
            log.debug("%s " + fmt, self.address_string(), *args)

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self, limit: int = MAX_BODY) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > limit:
                raise ApiError(413, "body_too_large")
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                data = json.loads(raw)
            except ValueError:
                raise ApiError(400, "bad_json") from None
            if not isinstance(data, dict):
                raise ApiError(400, "bad_json", "body must be a JSON object")
            return data

        def _auth(self):
            header = self.headers.get("Authorization") or ""
            token = header[7:].strip() if header.lower().startswith("bearer ") else None
            return app.authenticate(token)

        def _dispatch(self, method: str) -> None:
            url = urlsplit(self.path)
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                payload = self._route(method, url.path, query)
                self._send_json(200, payload)
            except ApiError as exc:
                self._send_json(exc.status, exc.to_json())
            except Exception:  # noqa: BLE001 - never leak a traceback to the client
                log.exception("unhandled error on %s %s", method, url.path)
                self._send_json(500, {"ok": False, "error": "internal_error"})

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        # -- routes -------------------------------------------------------------

        def _route(self, method: str, path: str, query: dict[str, str]) -> dict[str, Any]:
            if method == "GET" and path == "/v0/health":
                return {"ok": True}
            delegate = self._auth()
            if method == "POST" and path == "/v0/hello":
                body = self._read_json()
                return app.hello(delegate, str(body.get("agent_name", "")))
            if method == "GET" and path == "/v0/room":
                return {"ok": True, "room": app.room_info(delegate)}
            if method == "GET" and path == "/v0/inbox":
                after = int(query.get("after", 0))
                wait = float(query.get("wait", 0))
                return {"ok": True, "events": app.inbox(delegate, after, wait)}
            if method == "GET" and path == "/v0/transcript":
                ritual_id = query.get("ritual_id")
                messages = app.transcript(int(query.get("limit", 40)), ritual_id=int(ritual_id) if ritual_id else None)
                return {"ok": True, "messages": messages}
            if method == "POST" and path == "/v0/say":
                body = self._read_json()
                turn_id = body.get("turn_id")
                return app.say(delegate, str(body.get("text", "")), int(turn_id) if turn_id is not None else None)
            if method == "POST" and path == "/v0/pass":
                body = self._read_json()
                if body.get("turn_id") is None:
                    raise ApiError(400, "missing_turn_id")
                return app.pass_turn(delegate, int(body["turn_id"]), body.get("reason"))
            if method == "POST" and path == "/v0/share":
                body = self._read_json(limit=MAX_SHARE_BODY)
                try:
                    content = base64.b64decode(str(body.get("content_b64", "")), validate=True)
                except (ValueError, binascii.Error):
                    raise ApiError(400, "bad_base64") from None
                turn_id = body.get("turn_id")
                return app.share(
                    delegate, str(body.get("filename", "")), content,
                    str(body.get("caption", "")), int(turn_id) if turn_id is not None else None,
                )
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", f"{method} {path}")

    return Handler


class ApiServer:
    """Owns the threaded HTTP server; ``start`` returns immediately."""

    def __init__(self, app: Concierge, host: str, port: int):
        self.httpd = ThreadingHTTPServer((host, port), make_handler(app))
        self.httpd.daemon_threads = True
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="http", daemon=True)

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    def start(self) -> None:
        self._thread.start()
        log.info("delegate API listening on http://%s:%s", *self.httpd.server_address[:2])

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
