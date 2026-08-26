"""The delegate daemon loop: poll inbox → think (adapter) → say.

It keeps three things on disk under the delegate directory:
``state/state.json`` (inbox cursor, cumulative cost), ``memory/room-map.md``
(the agent's private notes, written by the daemon from the agent's output) and
nothing else. The agent itself never touches the filesystem.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .adapters.base import Adapter, AdapterError
from .client import ConciergeClient, ConciergeError, ConciergeUnreachable
from .config import DelegateConfig
from .prompt import (
    REPLY,
    SILENCE,
    build_memory_prompt,
    build_triage_prompt,
    build_reaction_prompt,
    build_system_prompt,
    build_turn_prompt,
    format_message,
    format_transcript,
)

log = logging.getLogger("tertulia.delegate")

ROOM_MAP_FILE = "room-map.md"


class DelegateDaemon:
    def __init__(
        self,
        cfg: DelegateConfig,
        client: ConciergeClient,
        adapter: Adapter,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.cfg = cfg
        self.client = client
        self.adapter = adapter
        self.clock = clock
        self.sleep = sleep
        self.stop = False
        self.my_id: int | None = None
        self.room: dict[str, Any] = {}
        self._state_path = cfg.state_dir / "state.json"
        self._state = self._load_state()

    # --------------------------------------------------------------- lifecycle

    def connect(self) -> None:
        """Say hello to the concierge, retrying until it is reachable."""
        backoff = 2.0
        while not self.stop:
            try:
                reply = self.client.hello(self.cfg.agent_name)
            except ConciergeError as exc:
                if exc.status == 401:
                    log.error("token rejected by the concierge (%s); stopping", exc)
                    self.stop = True
                    return
                log.warning("concierge not ready (%s); retrying in %.0fs", exc, backoff)
                self.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            self.my_id = int(reply["delegate"]["id"])
            self.room = reply["room"]
            log.info(
                "connected as %s (delegate of %s) to room %r; language=%s; delegates=%s",
                self.cfg.agent_name, reply["delegate"]["owner_name"], self.room.get("name"),
                self.room.get("language"), [d["agent_name"] for d in self.room.get("delegates", [])],
            )
            return

    def run_forever(self, *, wait: float = 25.0) -> None:
        self.connect()
        while not self.stop:
            try:
                self.run_once(wait=wait)
            except ConciergeUnreachable as exc:
                log.warning("concierge unreachable (%s); reconnecting", exc.message)
                self.sleep(5)
                self.connect()
            except ConciergeError as exc:
                if exc.status == 401:
                    log.error("token rejected by the concierge (%s); stopping", exc)
                    return
                log.warning("concierge error: %s", exc)
                self.sleep(2)
            except Exception:  # noqa: BLE001 - keep the daemon alive
                log.exception("unexpected error in daemon loop")
                self.sleep(5)

    def run_once(self, *, wait: float = 25.0) -> bool:
        """One poll cycle. Returns True if a batch was processed."""
        events = self.client.inbox(self.cursor, wait)
        if not events:
            return False
        if self.cfg.behaviour.batch_settle_seconds > 0:
            self.sleep(self.cfg.behaviour.batch_settle_seconds)
            events += self.client.inbox(events[-1]["seq"], 0)
        try:
            self._process_batch(events)
        finally:
            # Always advance: a batch that blew up is logged, not retried forever.
            self.cursor = events[-1]["seq"]
            self._save_state()
        return True

    # ------------------------------------------------------------------ state

    @property
    def cursor(self) -> int:
        return int(self._state.get("cursor", 0))

    @cursor.setter
    def cursor(self, value: int) -> None:
        self._state["cursor"] = int(value)

    def _load_state(self) -> dict[str, Any]:
        if self._state_path.exists():
            try:
                return json.loads(self._state_path.read_text(encoding="utf-8"))
            except ValueError:
                log.warning("state file unreadable; starting fresh")
        return {"cursor": 0, "total_cost_usd": 0.0, "calls": 0}

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(self._state, indent=2), encoding="utf-8")

    def _read(self, path: Path) -> str:
        return path.read_text(encoding="utf-8") if path.exists() else ""

    @property
    def room_map_path(self) -> Path:
        return self.cfg.memory_dir / ROOM_MAP_FILE

    # -------------------------------------------------------------- thinking

    def _roster(self) -> str:
        """Current delegates, so the agent never addresses someone who left.

        (25-aug-2026: Faro replied to revoked test delegates because the
        transcript still carried their messages and nothing said who was
        actually present.)
        """
        lines = []
        for d in self.room.get("delegates", []):
            me = " — you" if d.get("id") == self.my_id else ""
            lines.append(f"- {d.get('agent_name')} (delegate of {d.get('owner_name')}){me}")
        return "\n".join(lines)

    def _system_prompt(self) -> str:
        return build_system_prompt(
            agent_name=self.cfg.agent_name,
            owner_name=self.cfg.owner_name,
            personality=self.cfg.personality,
            profile=self._read(self.cfg.profile_path),
            room_map=self._read(self.room_map_path),
            room_name=str(self.room.get("name", "Tertulia")),
            language=str(self.room.get("language", "es")),
            roster=self._roster(),
        )

    def _complete(self, prompt: str, *, timeout: float | None = None, model: str | None = None) -> str | None:
        """Run the adapter; returns the text or None if it failed."""
        try:
            result = self.adapter.complete(
                system_prompt=self._system_prompt(), prompt=prompt, timeout=timeout, model=model
            )
        except AdapterError as exc:
            log.error("adapter failed: %s", exc)
            return None
        self._state["calls"] = int(self._state.get("calls", 0)) + 1
        if result.cost_usd:
            self._state["total_cost_usd"] = round(float(self._state.get("total_cost_usd", 0.0)) + result.cost_usd, 6)
            log.info("adapter call: %.4f USD (total %.4f USD over %d calls)",
                     result.cost_usd, self._state["total_cost_usd"], self._state["calls"])
        return result.text

    def _clip(self, text: str) -> str:
        """Trim to the room's message limit rather than get a 400 from the concierge."""
        limit = int((self.room.get("limits") or {}).get("max_message_chars", 2000))
        text = text.strip()
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    def _transcript(self, *, ritual_id: int | None = None, limit: int | None = None) -> str:
        messages = self.client.transcript(limit or self.cfg.behaviour.transcript_window, ritual_id=ritual_id)
        return format_transcript(messages, my_id=self.my_id, language=str(self.room.get("language", "es")))

    # ----------------------------------------------------------- processing

    def _notify(self, events: list[dict[str, Any]]) -> None:
        """Pipe the batch (JSON on stdin) to ``notify_command``, fire-and-forget.

        The command is the owner's own config (same trust level as the adapter
        command); a broken observer must never take the daemon down.
        """
        if not self.cfg.notify_command:
            return
        try:
            proc = subprocess.Popen(
                self.cfg.notify_command, shell=True, cwd=self.cfg.base_dir,
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
            )
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(events, ensure_ascii=False).encode("utf-8"))
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            log.warning("notify_command failed", exc_info=True)

    def _process_batch(self, events: list[dict[str, Any]]) -> None:
        self._notify(events)
        now = self.clock()
        turns = [e for e in events if e["kind"] == "turn"]
        closed = [e for e in events if e["kind"] == "ritual_closed"]
        messages = [e for e in events if e["kind"] == "room_message"]
        log.debug("batch: %d turn(s), %d message(s), %d ritual close(s)", len(turns), len(messages), len(closed))

        for ev in turns:
            self._handle_turn(ev, now)
        for ev in closed:
            self._handle_ritual_closed(ev)
        if messages and not turns:
            self._handle_messages(messages, now)

    def _handle_turn(self, ev: dict[str, Any], now: float) -> None:
        remaining = float(ev["deadline"]) - now
        if remaining <= 5:
            log.warning("turn %s already expired (%.0fs late); skipping", ev["turn_id"], -remaining)
            return
        log.info("turn %s: ritual=%s round=%s (%.0fs left)", ev["turn_id"], ev.get("ritual_id"), ev["round_id"], remaining)
        prompt = build_turn_prompt(
            transcript=self._transcript(),
            ritual=str(ev.get("ritual") or ev.get("ritual_id")),
            round_id=str(ev["round_id"]),
            instruction=str(ev["instruction"]),
        )
        text = self._complete(prompt, timeout=min(self.cfg.adapter.timeout_seconds, max(10.0, remaining - 5)))
        # Distinct pass reasons: they reach the concierge log, and "adapter
        # failed" vs "chose silence" is the difference between a broken member
        # setup (fix it) and a quiet agent (fine). Chasqui's mute welcome
        # (25-aug-2026) was undiagnosable remotely without this.
        if text is None:
            self._pass(int(ev["turn_id"]), "adapter failed")
            return
        if not text.strip() or text.strip() == SILENCE:
            self._pass(int(ev["turn_id"]), "agent chose silence")
            return
        text = self._clip(text)
        try:
            self.client.say(text, turn_id=int(ev["turn_id"]))
            log.info("said (turn %s): %s", ev["turn_id"], text[:100].replace("\n", " "))
        except ConciergeError as exc:
            log.warning("turn %s rejected by concierge: %s", ev["turn_id"], exc)

    def _pass(self, turn_id: int, reason: str) -> None:
        try:
            self.client.pass_turn(turn_id, reason)
            log.info("passed turn %s (%s)", turn_id, reason)
        except ConciergeError as exc:
            log.warning("could not pass turn %s: %s", turn_id, exc)

    def _reactable(self, ev: dict[str, Any], now: float) -> bool:
        m = ev["message"]
        if now - float(ev.get("at", m.get("at", now))) > self.cfg.behaviour.react_max_age_seconds:
            return False
        if m.get("sender_kind") == "concierge" or m.get("ritual_id") is not None:
            return False
        if ev.get("addressed_to_you"):
            return True
        if m.get("sender_kind") == "human":
            return self.cfg.behaviour.react_to_humans
        if m.get("sender_kind") == "delegate":
            return self.cfg.behaviour.react_to_delegates
        return False

    def _handle_messages(self, events: list[dict[str, Any]], now: float) -> None:
        fresh = [e for e in events if self._reactable(e, now)]
        if not fresh:
            return
        room = self.client.room()
        self.room = room
        if room.get("ritual"):
            log.info("%d new message(s) but a ritual is running; staying quiet", len(fresh))
            return
        remaining = int(room.get("you", {}).get("spontaneous_remaining_24h", 0))
        if remaining <= 0:
            log.info("%d new message(s) but no spontaneous quota left; staying quiet", len(fresh))
            return
        language = str(room.get("language", "es"))
        new_text = "\n".join(format_message(e["message"], my_id=self.my_id, language=language) for e in fresh)
        transcript = self._transcript()
        if self.cfg.adapter.fast_model:
            # Cheap gate first: the voice model only runs when there is something to say.
            verdict = self._complete(
                build_triage_prompt(transcript=transcript, new_messages=new_text, remaining=remaining),
                model=self.cfg.adapter.fast_model,
            )
            if not verdict or REPLY not in verdict:
                log.info("triage: staying quiet after %d new message(s)", len(fresh))
                return
        prompt = build_reaction_prompt(transcript=transcript, new_messages=new_text, remaining=remaining)
        text = self._complete(prompt)
        if not text or text.strip().startswith(SILENCE):
            log.info("decided to stay quiet after %d new message(s)", len(fresh))
            return
        text = self._clip(text)
        try:
            self.client.say(text)
            log.info("said: %s", text[:100].replace("\n", " "))
        except ConciergeError as exc:
            log.info("spontaneous message rejected by concierge: %s", exc)

    def _handle_ritual_closed(self, ev: dict[str, Any]) -> None:
        for action in ev.get("after_close") or []:
            if action.get("action") != "update_memory":
                continue
            ritual_id = ev.get("ritual_id")
            transcript = self._transcript(ritual_id=int(ritual_id) if ritual_id is not None else None, limit=200)
            prompt = build_memory_prompt(
                current_notes=self._read(self.room_map_path),
                ritual_transcript=transcript,
                instruction=str(action.get("instruction", "")),
            )
            text = self._complete(prompt)
            if text and text.strip() and text.strip() != SILENCE:
                self.room_map_path.parent.mkdir(parents=True, exist_ok=True)
                self.room_map_path.write_text(text.strip() + "\n", encoding="utf-8")
                log.info("room map updated (%d chars) -> %s", len(text), self.room_map_path)
