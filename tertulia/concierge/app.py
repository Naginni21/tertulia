"""The concierge application: Telegram in, delegates out, rituals, brakes.

Threads:
* Telegram poller (``poll_telegram_forever``) → ``handle_update``.
* HTTP server threads → the ``hello/room/inbox/transcript/say/pass_turn`` API.
* Main loop → ``tick`` every ~0.5 s (rituals, deadlines, pending joins).

All state lives in the ``Store``; ``_lock`` guards the in-memory ritual run.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .config import ConciergeConfig
from .i18n import Strings
from .limits import Limits, SpontaneousSnapshot, check_spontaneous, check_text
from .rituals import RitualRun, RitualSpec, render
from .store import Delegate, Message, Store
from .telegram import TelegramClient, TelegramError, html_escape

log = logging.getLogger("tertulia.concierge")

DAY = 24 * 3600
# A scheduled ritual missed by less than this (nobody online at the time, or
# the concierge was down) still runs when someone shows up; older ones are skipped.
CATCH_UP_SECONDS = 6 * 3600
# Shared files: well under Telegram's 50MB bot limit, generous for a skill/KB kit.
MAX_SHARE_BYTES = 20 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = "", **extra: Any):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code
        self.extra = extra

    def to_json(self) -> dict[str, Any]:
        return {"ok": False, "error": self.code, "message": self.message, **self.extra}


@dataclass
class RitualRequest:
    spec: RitualSpec
    newcomers: list[int]


class Concierge:
    def __init__(
        self,
        cfg: ConciergeConfig,
        store: Store,
        telegram: TelegramClient,
        rituals: dict[str, RitualSpec],
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.cfg = cfg
        self.store = store
        self.tg = telegram
        self.rituals = rituals
        self.clock = clock
        self.t = Strings(cfg.room.language)
        self._tz = ZoneInfo(cfg.room.timezone) if cfg.room.timezone else None
        self.limits = Limits(
            spontaneous_per_24h=cfg.limits.spontaneous_per_24h,
            min_gap_seconds=cfg.limits.min_gap_seconds,
            max_consecutive_delegate_messages=cfg.limits.max_consecutive_delegate_messages,
            max_message_chars=cfg.limits.max_message_chars,
        )
        self._lock = threading.RLock()
        self._inbox_cv = threading.Condition()
        self._ritual: RitualRun | None = None
        self._ritual_queue: list[RitualRequest] = []
        self._pending_newcomers: dict[int, float] = {}  # delegate_id -> joined_at
        self.stop_event = threading.Event()

    # ------------------------------------------------------------------ startup

    def start(self) -> None:
        aborted = self.store.abort_running_rituals(now=self.clock())
        if aborted:
            log.warning("aborted %d ritual(s) left running by a previous process", aborted)
        log.info(
            "concierge up: room=%r language=%s chat_id=%s rituals=%s",
            self.cfg.room.name, self.cfg.room.language, self.cfg.telegram.chat_id, sorted(self.rituals),
        )
        if self.cfg.server.host not in ("127.0.0.1", "localhost", "::1"):
            log.warning(
                "server.host=%s may be reachable from the internet; the API speaks plain HTTP, "
                "so delegate tokens travel unencrypted — put TLS in front (Caddy, a VPN or a tunnel)",
                self.cfg.server.host,
            )
        if self._scheduled() and self._tz is None:
            log.warning(
                "rituals %s are scheduled but room.timezone is not set; they will never run",
                [s.id for s in self._scheduled()],
            )

    # ------------------------------------------------------------- telegram in

    def poll_telegram_forever(self) -> None:
        offset_raw = self.store.kv_get("telegram_update_offset")
        offset = int(offset_raw) if offset_raw else None
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                updates = self.tg.get_updates(offset, timeout=25)
                backoff = 1.0
            except Exception as exc:  # noqa: BLE001 - network hiccups must not kill the poller
                log.warning("getUpdates failed (%s); retrying in %.0fs", exc, backoff)
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue
            for update in updates:
                offset = int(update["update_id"]) + 1
                try:
                    self.handle_update(update)
                except Exception:  # noqa: BLE001
                    log.exception("error handling update %s", update.get("update_id"))
                self.store.kv_set("telegram_update_offset", str(offset))

    def handle_update(self, update: dict[str, Any]) -> None:
        msg = update.get("message")
        if not msg:
            return
        chat = msg.get("chat") or {}
        if int(chat.get("id", 0)) != self.cfg.telegram.chat_id:
            log.info("ignoring message from chat %s (%s) — not the room", chat.get("id"), chat.get("title") or chat.get("type"))
            return
        migrated = msg.get("migrate_to_chat_id")
        if migrated:
            log.error(
                "Telegram converted the group to a supergroup: new chat_id is %s. "
                "Update telegram.chat_id (or TERTULIA_CHAT_ID) and restart.", migrated,
            )
            return
        text = msg.get("text")
        if not text:
            return  # v0: text only (photos/files are v2)
        sender = msg.get("from") or {}
        if sender.get("is_bot"):
            return
        name = sender.get("first_name") or sender.get("username") or "someone"
        user_id = int(sender.get("id", 0))
        if text.startswith("/"):
            self._handle_command(text, name, user_id)
            return
        message = self.store.add_message(
            at=float(msg.get("date") or self.clock()),
            sender_kind="human",
            sender_name=name,
            text=text,
            telegram_message_id=msg.get("message_id"),
        )
        log.info("room <- %s: %s", name, text[:80])
        self._broadcast_message(message)

    def _handle_command(self, text: str, name: str, user_id: int) -> None:
        command = text.split()[0].split("@")[0].lower()
        if command == "/status":
            self._post_plain(self.status_text())
        elif command == "/welcome":
            if user_id not in self.cfg.telegram.admin_user_ids:
                self._post_plain(self.t("not_admin"))
                return
            joined = [d.id for d in self.store.delegates(joined_only=True)]
            if not joined:
                self._post_plain(self.t("no_delegates"))
                return
            with self._lock:
                busy = self._ritual is not None
                self._ritual_queue.append(RitualRequest(self.rituals["welcome"], newcomers=joined))
            if busy:
                self._post_plain(self.t("ritual_busy"))
            log.info("/welcome requested by %s (%s)", name, user_id)
        elif command == "/ritual":
            if user_id not in self.cfg.telegram.admin_user_ids:
                self._post_plain(self.t("not_admin"))
                return
            parts = text.split()
            spec = self.rituals.get(parts[1].lower()) if len(parts) > 1 else None
            if spec is None:
                self._post_plain(self.t("unknown_ritual", rituals=", ".join(sorted(self.rituals))))
                return
            joined = [d.id for d in self.store.delegates(joined_only=True)]
            if not joined:
                self._post_plain(self.t("no_delegates"))
                return
            with self._lock:
                busy = self._ritual is not None
                self._ritual_queue.append(RitualRequest(spec, newcomers=joined if spec.id == "welcome" else []))
            if busy:
                self._post_plain(self.t("ritual_busy"))
            log.info("/ritual %s requested by %s (%s)", spec.id, name, user_id)
        # unknown commands are ignored on purpose (other bots may share the group)

    def status_text(self) -> str:
        now = self.clock()
        lines = [self.t("status_title")]
        for d in self.store.delegates():
            if not d.joined_at:
                continue
            used = self.store.spontaneous_count(d.id, since=now - DAY)
            lines.append(
                self.t("status_delegate", online=self.t("online" if self._is_online(d, now) else "offline"), label=self._label(d))
                + " — " + self.t("status_quota", used=used, max=self.limits.spontaneous_per_24h)
            )
        if len(lines) == 1:
            lines.append(self.t("no_delegates"))
        with self._lock:
            ritual = self._ritual
        lines.append(self.t("status_ritual", ritual=ritual.spec.name) if ritual else self.t("status_no_ritual"))
        if self._tz is not None and self._scheduled():
            tz = self._tz
            nxt = min(self._scheduled(), key=lambda s: s.schedule.after(now, tz))  # type: ignore[union-attr]
            lines.append(self.t("status_next", ritual=nxt.name, when=self._when(nxt.schedule.after(now, tz))))  # type: ignore[union-attr]
        return "\n".join(lines)

    # ------------------------------------------------------------ delegates API

    def authenticate(self, token: str | None) -> Delegate:
        if not token:
            raise ApiError(401, "missing_token", "Authorization: Bearer <token> required")
        delegate = self.store.delegate_by_token(token)
        if delegate is None:
            raise ApiError(401, "invalid_token", "unknown or revoked token")
        return delegate

    def hello(self, delegate: Delegate, agent_name: str) -> dict[str, Any]:
        agent_name = (agent_name or "").strip()
        if not agent_name or len(agent_name) > 40:
            raise ApiError(400, "bad_agent_name", "agent_name must be 1-40 characters")
        now = self.clock()
        first_time = self.store.register_hello(delegate.id, agent_name, now=now)
        delegate = self.store.delegate(delegate.id)
        assert delegate is not None
        if first_time:
            log.info("delegate joined: %s", self._label(delegate))
            if "welcome" in self.rituals and self.rituals["welcome"].trigger == "delegate_joined":
                with self._lock:
                    self._pending_newcomers[delegate.id] = now
        else:
            log.info("delegate back: %s", self._label(delegate))
        return {"ok": True, "delegate": delegate.to_api(online=True), "room": self.room_info(delegate)}

    def _spontaneous_blocked_by(self, me: Delegate, now: float) -> str | None:
        last_own = self.store.last_message_at(me.id)
        snap = SpontaneousSnapshot(
            sent_last_24h=self.store.spontaneous_count(me.id, since=now - DAY),
            seconds_since_last_own=(now - last_own) if last_own is not None else None,
            consecutive_delegate_tail=self.store.consecutive_delegate_tail(),
            ritual_running=self._ritual is not None,
        )
        return check_spontaneous(self.limits, snap)

    def room_info(self, me: Delegate) -> dict[str, Any]:
        now = self.clock()
        with self._lock:
            ritual = self._ritual
        used = self.store.spontaneous_count(me.id, since=now - DAY)
        return {
            "name": self.cfg.room.name,
            "language": self.cfg.room.language,
            "delegates": [d.to_api(online=self._is_online(d, now)) for d in self.store.delegates(joined_only=True)],
            "ritual": {"id": ritual.ritual_id, "name": ritual.spec.id} if ritual else None,
            "limits": {
                "spontaneous_per_24h": self.limits.spontaneous_per_24h,
                "max_message_chars": self.limits.max_message_chars,
            },
            "you": {
                "id": me.id,
                "agent_name": me.agent_name,
                "owner_name": me.owner_name,
                "spontaneous_remaining_24h": max(0, self.limits.spontaneous_per_24h - used),
                # Why a spontaneous message would be rejected RIGHT NOW (or
                # null): lets the daemon skip composing (an LLM call) for a
                # message the concierge would bounce anyway.
                "spontaneous_blocked_by": self._spontaneous_blocked_by(me, now),
            },
        }

    def inbox(self, delegate: Delegate, after: int, wait: float) -> list[dict[str, Any]]:
        """Long-poll: events with seq > ``after``; blocks up to ``wait`` seconds."""
        wait = max(0.0, min(wait, float(self.cfg.server.long_poll_seconds)))
        deadline = self.clock() + wait
        self.store.touch(delegate.id, now=self.clock())
        while True:
            events = self.store.events_after(delegate.id, after)
            remaining = deadline - self.clock()
            if events or remaining <= 0 or self.stop_event.is_set():
                return events
            with self._inbox_cv:
                self._inbox_cv.wait(timeout=min(remaining, 5.0))

    def transcript(self, limit: int, *, ritual_id: int | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        return [m.to_api() for m in self.store.recent_messages(limit, ritual_id=ritual_id)]

    def _gate_outgoing(self, delegate: Delegate, turn_id: int | None, now: float,
                       *, origin_owner: bool = False) -> int | None:
        """Common gates for anything a delegate posts (text or file); claims the
        turn so the ticker does not expire it mid-send. Returns the ritual id.

        ``origin_owner`` marks the message as the owner speaking through their
        delegate (an outbox note): humans have no spontaneous quota, so neither
        does their deferred speech. The flag is claimed by the owner's own
        daemon — same trust level as everything else on the owner's machine.
        """
        if not delegate.agent_name:
            raise ApiError(409, "hello_first", "call /v0/hello before speaking")
        if turn_id is None and origin_owner:
            if self._ritual is not None:
                raise ApiError(429, "ritual_running", "a ritual is running; owner notes wait")
            return None
        if turn_id is not None:
            with self._lock:
                turn = self.store.turn(turn_id)
                if turn is None or turn.delegate_id != delegate.id:
                    raise ApiError(404, "unknown_turn")
                if turn.status != "open":
                    raise ApiError(409, "turn_closed", f"turn is {turn.status}")
                if now > turn.deadline_at:
                    raise ApiError(409, "turn_expired")
                self.store.set_turn_status(turn_id, "answering")
            return turn.ritual_id
        last_own = self.store.last_message_at(delegate.id)
        snap = SpontaneousSnapshot(
            sent_last_24h=self.store.spontaneous_count(delegate.id, since=now - DAY),
            seconds_since_last_own=(now - last_own) if last_own is not None else None,
            consecutive_delegate_tail=self.store.consecutive_delegate_tail(),
            ritual_running=self._ritual is not None,
        )
        reason = check_spontaneous(self.limits, snap)
        if reason:
            raise ApiError(429, reason, f"spontaneous message rejected: {reason}")
        return None

    def _record_outgoing(
        self, delegate: Delegate, *, now: float, text: str, tg_id: int | None,
        ritual_id: int | None, turn_id: int | None, origin_owner: bool = False,
    ) -> dict[str, Any]:
        message = self.store.add_message(
            at=now,
            sender_kind="delegate",
            sender_name=delegate.agent_name,
            sender_owner=delegate.owner_name,
            delegate_id=delegate.id,
            text=text,
            telegram_message_id=tg_id,
            ritual_id=ritual_id,
            turn_id=turn_id,
            origin_owner=origin_owner,
        )
        log.info("room <- %s: %s", self._label(delegate), text[:80])
        if turn_id is not None:
            with self._lock:
                self.store.set_turn_status(turn_id, "answered")
                if self._ritual is not None:
                    self._ritual.on_answer(turn_id)
        self._broadcast_message(message, exclude=delegate.id)
        return {"ok": True, "message_id": message.id}

    def say(self, delegate: Delegate, text: str, turn_id: int | None,
            origin_owner: bool = False) -> dict[str, Any]:
        text = (text or "").strip()
        reason = check_text(self.limits, text)
        if reason:
            raise ApiError(400, reason)
        now = self.clock()
        ritual_id = self._gate_outgoing(delegate, turn_id, now, origin_owner=origin_owner)
        try:
            tg_id = self._send_delegate_message(delegate, text)
        except TelegramError as exc:
            if turn_id is not None:
                self.store.set_turn_status(turn_id, "open")
            raise ApiError(502, "telegram_error", str(exc)) from exc
        return self._record_outgoing(
            delegate, now=now, text=text, tg_id=tg_id, ritual_id=ritual_id, turn_id=turn_id,
            origin_owner=origin_owner,
        )

    def share(self, delegate: Delegate, filename: str, content: bytes, caption: str, turn_id: int | None,
              origin_owner: bool = False) -> dict[str, Any]:
        """A delegate posts a file from its owner-approved catalogue.

        Same gates as ``say``: a share is a room message with an attachment, so
        it spends the same turn or spontaneous quota. The concierge trusts the
        daemon to only send catalogue files — the API just bounds size/name.
        """
        filename = (filename or "").strip()
        if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
            raise ApiError(400, "bad_filename")
        if not content:
            raise ApiError(400, "empty_file")
        if len(content) > MAX_SHARE_BYTES:
            raise ApiError(413, "file_too_large", f"max {MAX_SHARE_BYTES} bytes")
        # Telegram caps captions at 1024 chars, below the room's text limit.
        caption = (caption or "").strip()[:1000]
        now = self.clock()
        ritual_id = self._gate_outgoing(delegate, turn_id, now, origin_owner=origin_owner)
        header = self.t("delegate_header", agent=delegate.display_name, owner=delegate.owner_name)
        caption_html = f"<b>{html_escape(header)}</b>"
        if caption:
            caption_html += f"\n{html_escape(caption)}"
        try:
            result = self.tg.send_document(self.cfg.telegram.chat_id, filename, content, caption=caption_html)
        except TelegramError as exc:
            if turn_id is not None:
                self.store.set_turn_status(turn_id, "open")
            raise ApiError(502, "telegram_error", str(exc)) from exc
        tg_id = result.get("message_id") if isinstance(result, dict) else None
        text = self.t("share", filename=filename) + (f": {caption}" if caption else "")
        return self._record_outgoing(
            delegate, now=now, text=text, tg_id=tg_id, ritual_id=ritual_id, turn_id=turn_id,
            origin_owner=origin_owner,
        )

    def pass_turn(self, delegate: Delegate, turn_id: int, reason: str | None = None) -> dict[str, Any]:
        with self._lock:
            turn = self.store.turn(turn_id)
            if turn is None or turn.delegate_id != delegate.id:
                raise ApiError(404, "unknown_turn")
            if turn.status != "open":
                raise ApiError(409, "turn_closed", f"turn is {turn.status}")
            self.store.set_turn_status(turn_id, "passed")
            if self._ritual is not None:
                self._ritual.on_answer(turn_id)
        log.info("%s passed turn %s (%s)", self._label(delegate), turn_id, reason or "no reason")
        self.post_concierge(self.t("passed", label=self._label(delegate)), ritual_id=turn.ritual_id)
        return {"ok": True}

    # --------------------------------------------------------------- main loop

    def tick(self) -> None:
        now = self.clock()
        with self._lock:
            self._schedule_rituals(now)
            self._start_pending_ritual(now)
            run = self._ritual
            if run is None:
                return
            try:
                done = run.tick(now, self)
            except TelegramError:
                log.exception("telegram error while running ritual %s; will retry", run.ritual_id)
                return
            self.store.save_ritual_state(run.ritual_id, run.to_state())
            if done:
                self.store.finish_ritual(run.ritual_id, "done", now=now)
                log.info("ritual %s (%s) finished", run.ritual_id, run.spec.id)
                self._ritual = None

    def _start_pending_ritual(self, now: float) -> None:
        if self._ritual is not None:
            return
        if self._pending_newcomers:
            last_join = max(self._pending_newcomers.values())
            if now - last_join >= self.cfg.room.join_grace_seconds:
                newcomers = sorted(self._pending_newcomers, key=self._pending_newcomers.get)  # type: ignore[arg-type]
                self._pending_newcomers.clear()
                self._ritual_queue.insert(0, RitualRequest(self.rituals["welcome"], newcomers=newcomers))
        if not self._ritual_queue:
            return
        req = self._ritual_queue.pop(0)
        participants = [d.id for d in self.store.delegates(joined_only=True)]
        if not participants:
            return
        state_preview = {"spec_id": req.spec.id}
        ritual_id = self.store.create_ritual(req.spec.id, state_preview, now=now)
        self._ritual = RitualRun(ritual_id=ritual_id, spec=req.spec, participants=participants, newcomers=req.newcomers)
        log.info("ritual %s (%s) started: participants=%s newcomers=%s", ritual_id, req.spec.id, participants, req.newcomers)

    # ------------------------------------------------------------- schedules

    def _scheduled(self) -> list[RitualSpec]:
        return [s for s in self.rituals.values() if s.trigger == "schedule" and s.schedule is not None]

    def _when(self, at: float) -> str:
        assert self._tz is not None
        local = datetime.fromtimestamp(at, self._tz)
        return self.t("when", weekday=self.t(f"weekday_{local.weekday()}"), time=local.strftime("%H:%M"))

    def _schedule_rituals(self, now: float) -> None:
        """Queue each scheduled ritual once per occurrence; post its reminder once.

        Both marks live in the store (``ritual_last_run:<id>``,
        ``ritual_reminded:<id>``) so a restart neither repeats nor forgets.
        """
        if self._tz is None:
            return
        for spec in self._scheduled():
            assert spec.schedule is not None
            due = spec.schedule.at_or_before(now, self._tz)
            last_run = float(self.store.kv_get(f"ritual_last_run:{spec.id}") or 0)
            if due > last_run and now - due <= CATCH_UP_SECONDS:
                queued = any(r.spec.id == spec.id for r in self._ritual_queue)
                online = any(self._is_online(d, now) for d in self.store.delegates(joined_only=True))
                # Nobody online: keep trying within the catch-up window rather
                # than posting an opening nobody answers.
                if not queued and online:
                    self._ritual_queue.append(RitualRequest(spec, newcomers=[]))
                    self.store.kv_set(f"ritual_last_run:{spec.id}", str(due))
                    log.info("ritual %s is due (%s); queued", spec.id, self._when(due))
            if spec.remind and spec.remind_before_minutes > 0:
                nxt = spec.schedule.after(now, self._tz)
                if nxt - now <= spec.remind_before_minutes * 60 and self.store.kv_get(f"ritual_reminded:{spec.id}") != str(nxt):
                    self.store.kv_set(f"ritual_reminded:{spec.id}", str(nxt))
                    self.post_concierge(render(spec.remind, room=self.cfg.room.name, when=self._when(nxt)), ritual_id=None)
                    self.broadcast("ritual_soon", {"ritual": spec.id, "name": spec.name, "at": nxt})
                    log.info("reminder for ritual %s posted (%s)", spec.id, self._when(nxt))

    # ---------------------------------------------------------------- RoomPort

    def post_concierge(self, text: str, *, ritual_id: int | None) -> None:
        tg_id = self._post_plain(text)
        message = self.store.add_message(
            at=self.clock(), sender_kind="concierge", sender_name="concierge", text=text,
            telegram_message_id=tg_id, ritual_id=ritual_id,
        )
        self._broadcast_message(message)

    def broadcast(self, kind: str, payload: dict[str, Any]) -> None:
        now = self.clock()
        for d in self.store.delegates(joined_only=True):
            self.store.push_event(d.id, kind, payload, at=now)
        self._wake_inboxes()

    def issue_turn(self, *, ritual_id: int, ritual: str, round_id: str, delegate_id: int, instruction: str, deadline: float) -> int:
        now = self.clock()
        turn = self.store.create_turn(
            ritual_id=ritual_id, round_id=round_id, delegate_id=delegate_id, issued_at=now, deadline_at=deadline
        )
        self.store.push_event(
            delegate_id,
            "turn",
            {"turn_id": turn.id, "ritual_id": ritual_id, "ritual": ritual, "round_id": round_id,
             "instruction": instruction, "deadline": deadline},
            at=now,
        )
        self._wake_inboxes()
        d = self.store.delegate(delegate_id)
        log.info("turn %s issued to %s (round %s)", turn.id, self._label(d) if d else delegate_id, round_id)
        return turn.id

    def turn_status(self, turn_id: int) -> str:
        turn = self.store.turn(turn_id)
        return turn.status if turn else "unknown"

    def close_turn(self, turn_id: int, status: str) -> None:
        self.store.set_turn_status(turn_id, status)

    def delegate_online(self, delegate_id: int) -> bool:
        d = self.store.delegate(delegate_id)
        return d is not None and self._is_online(d, self.clock())

    def delegate_label(self, delegate_id: int) -> str:
        d = self.store.delegate(delegate_id)
        return self._label(d) if d else f"delegate#{delegate_id}"

    def note(self, key: str, **fmt: object) -> str:
        return self.t(key, **fmt)

    def join_names(self, names: list[str]) -> str:
        return self.t.join_names(names)

    def room_name(self) -> str:
        return self.cfg.room.name

    # ----------------------------------------------------------------- helpers

    def _is_online(self, d: Delegate, now: float) -> bool:
        return d.last_seen_at is not None and now - d.last_seen_at <= self.cfg.room.online_window_seconds

    def _label(self, d: Delegate) -> str:
        return self.t("delegate_label", agent=d.display_name, owner=d.owner_name)

    def _post_plain(self, text: str) -> int | None:
        result = self.tg.send_message(self.cfg.telegram.chat_id, html_escape(text))
        return result.get("message_id") if isinstance(result, dict) else None

    def _send_delegate_message(self, d: Delegate, text: str) -> int | None:
        header = self.t("delegate_header", agent=d.display_name, owner=d.owner_name)
        html = f"<b>{html_escape(header)}</b>\n{html_escape(text)}"
        result = self.tg.send_message(self.cfg.telegram.chat_id, html)
        return result.get("message_id") if isinstance(result, dict) else None

    def _broadcast_message(self, message: Message, *, exclude: int | None = None) -> None:
        now = self.clock()
        lowered = message.text.lower()
        for d in self.store.delegates(joined_only=True):
            if d.id == exclude:
                continue
            addressed = bool(d.agent_name) and d.agent_name.lower() in lowered
            payload = {"message": message.to_api(), "addressed_to_you": addressed}
            self.store.push_event(d.id, "room_message", payload, at=now)
        self._wake_inboxes()

    def _wake_inboxes(self) -> None:
        with self._inbox_cv:
            self._inbox_cv.notify_all()
