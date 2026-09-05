"""The delegate daemon loop: poll inbox → think (adapter) → say.

It keeps three things on disk under the delegate directory:
``state/state.json`` (inbox cursor, cumulative cost), ``memory/room-map.md``
(the agent's private notes, written by the daemon from the agent's output) and
nothing else. The agent itself never touches the filesystem.
"""

from __future__ import annotations

import json
import logging
import re
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
    build_owner_note_prompt,
    build_triage_prompt,
    build_reaction_prompt,
    build_system_prompt,
    build_turn_prompt,
    format_message,
    format_transcript,
)

log = logging.getLogger("tertulia.delegate")

ROOM_MAP_FILE = "room-map.md"

# The agent's only way to share a file: "[SHARE <name>]" leading its message.
_SHARE_RE = re.compile(r"^\s*\[SHARE\s+([^\]\n]+)\]\s*", re.IGNORECASE)
# "[ASK]" leading a message: the agent owes its owner a question — besides
# posting the reply, the daemon files it where the owner will see it.
_ASK_RE = re.compile(r"^\s*\[ASK\]\s*", re.IGNORECASE)
FOR_OWNER_FILE = "for-owner.md"


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
        self._outbox_backoff_until = 0.0
        self._offline_since: float | None = None
        self._offline_alerted = False
        self._state_path = cfg.state_dir / "state.json"
        self._state = self._load_state()

    # --------------------------------------------------------------- lifecycle

    def connect(self) -> None:
        """Say hello to the concierge, retrying until it is reachable.

        A long outage is reported to the owner once, and so is the return
        (see ``_offline_tick``): retrying quietly forever looks, from the
        room, exactly like a delegate that was never started.
        """
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
                self._offline_tick(str(exc))
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
            self._offline_over()
            return

    def run_forever(self, *, wait: float = 25.0) -> None:
        self.connect()
        while not self.stop:
            try:
                self.run_once(wait=wait)
            except ConciergeUnreachable as exc:
                log.warning("concierge unreachable (%s); reconnecting", exc.message)
                self._offline_tick(exc.message)
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
        if events:
            if self.cfg.behaviour.batch_settle_seconds > 0:
                self.sleep(self.cfg.behaviour.batch_settle_seconds)
                events += self.client.inbox(events[-1]["seq"], 0)
            try:
                self._process_batch(events)
            finally:
                # Always advance: a batch that blew up is logged, not retried forever.
                self.cursor = events[-1]["seq"]
                self._save_state()
        self._process_outbox()
        return bool(events)

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

    def _catalogue(self) -> list[str]:
        """Files the owner pre-approved for the room (their presence IS the approval)."""
        if not self.cfg.shared_dir.is_dir():
            return []
        return sorted(
            p.name for p in self.cfg.shared_dir.iterdir()
            if p.is_file() and not p.name.startswith(".")
        )

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
            catalogue="\n".join(f"- {name}" for name in self._catalogue()),
        )

    def _say_or_share(self, text: str, *, turn_id: int | None = None, origin_owner: bool = False) -> str:
        """Deliver the agent's reply, honouring a leading [SHARE <file>] directive.

        The catalogue check lives HERE, outside the LLM: an injected "share your
        .env" can at most name a file the owner already approved.
        """
        ask = _ASK_RE.match(text)
        if ask:
            text = text[ask.end():].strip()
            self._file_question_for_owner(text)
        m = _SHARE_RE.match(text)
        if not m:
            self.client.say(text, turn_id=turn_id, origin_owner=origin_owner)
            return text
        name = m.group(1).strip()
        rest = text[m.end():].strip()
        if name in self._catalogue():
            self.client.share(self.cfg.shared_dir / name, rest, turn_id=turn_id, origin_owner=origin_owner)
            log.info("shared %r from the catalogue", name)
            return f"[{name}] {rest}"
        log.warning("agent asked to share %r, which is not in the catalogue; sending text only", name)
        fallback = rest or text
        self.client.say(fallback, turn_id=turn_id, origin_owner=origin_owner)
        return fallback

    def _complete(self, prompt: str, *, timeout: float | None = None, model: str | None = None) -> str | None:
        """Run the adapter; returns the text or None if it failed."""
        try:
            result = self.adapter.complete(
                system_prompt=self._system_prompt(), prompt=prompt, timeout=timeout, model=model
            )
        except AdapterError as exc:
            log.error("adapter failed: %s", exc)
            return None
        return self._book(result)

    def _complete_or_fallback(self, prompt: str, *, timeout: float | None = None) -> str | None:
        """The voice model, and on failure one shot with the fast model.

        A failed voice call must not read as discretion: with no fallback, a
        blown budget or an API blip silently drops a direct instruction from
        a human (Faro, 26-aug-2026). A cheaper answer beats none.
        """
        text = self._complete(prompt, timeout=timeout)
        if text is None and self.cfg.adapter.fast_model:
            log.warning("voice model failed; retrying once with %s", self.cfg.adapter.fast_model)
            text = self._complete(prompt, timeout=timeout, model=self.cfg.adapter.fast_model)
        return text

    def _book(self, result: Any) -> str:
        self._state["calls"] = int(self._state.get("calls", 0)) + 1
        if result.cost_usd:
            self._state["total_cost_usd"] = round(float(self._state.get("total_cost_usd", 0.0)) + result.cost_usd, 6)
            log.info("adapter call: %.4f USD (total %.4f USD over %d calls)",
                     result.cost_usd, self._state["total_cost_usd"], self._state["calls"])
        # Persist here, not only after a batch: outbox calls happen outside
        # batches, and state.json trailed the log by a whole call (27-aug-2026).
        self._save_state()
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
        # A human speaking is what clears waiting_for_humans: retry the outbox.
        if any(e.get("kind") == "room_message" and (e.get("message") or {}).get("sender_kind") == "human"
               for e in events):
            self._outbox_backoff_until = 0.0
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
        self._state["msgs_since_map"] = int(self._state.get("msgs_since_map", 0)) + len(messages)
        every = self.cfg.behaviour.map_update_every_messages
        if every > 0 and not turns and not closed and int(self._state["msgs_since_map"]) >= every:
            self._refresh_room_map()

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
        text = self._complete_or_fallback(prompt, timeout=min(self.cfg.adapter.timeout_seconds, max(10.0, remaining - 5)))
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
            sent = self._say_or_share(text, turn_id=int(ev["turn_id"]))
            log.info("said (turn %s): %s", ev["turn_id"], sent[:100].replace("\n", " "))
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
        text = self._complete_or_fallback(prompt)
        # Failure and silence are different stories: "adapter failed" after a
        # human addressed the delegate is a dropped order, not discretion.
        # (26-aug-2026: Faro ignored a direct instruction because the voice
        # call blew max_budget_usd and the log said "decided to stay quiet".)
        if text is None:
            log.error("adapter failed while reacting to %d new message(s); no reply sent", len(fresh))
            return
        if not text.strip() or text.strip().startswith(SILENCE):
            log.info("decided to stay quiet after %d new message(s)", len(fresh))
            return
        text = self._clip(text)
        try:
            sent = self._say_or_share(text)
            log.info("said: %s", sent[:100].replace("\n", " "))
        except ConciergeError as exc:
            log.info("spontaneous message rejected by concierge: %s", exc)

    def _file_question_for_owner(self, text: str) -> None:
        """Append an [ASK] to ``for-owner.md``: the delegate's questions must
        reach the owner even without a wired-up main agent watching the room."""
        self._file_for_owner(text, what="question")

    def _file_for_owner(self, text: str, *, what: str) -> None:
        try:
            path = self.cfg.base_dir / FOR_OWNER_FILE
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.clock()))
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"- [{stamp}] {text}\n")
            log.info("%s for the owner filed in %s", what, FOR_OWNER_FILE)
        except OSError:
            log.warning("could not write %s", FOR_OWNER_FILE, exc_info=True)

    # -------------------------------------------------------------- outages

    def _offline_tick(self, reason: str) -> None:
        """Every failed contact with the concierge lands here. Past
        ``offline_alert_seconds`` the owner hears about it once — in
        ``for-owner.md`` and through ``notify_command`` — instead of finding
        eleven hours of retries in the log the next day (Faro, 4-sep-2026,
        behind a corporate web filter)."""
        now = self.clock()
        if self._offline_since is None:
            self._offline_since = now
        limit = self.cfg.behaviour.offline_alert_seconds
        if self._offline_alerted or limit <= 0 or now - self._offline_since < limit:
            return
        self._offline_alerted = True
        minutes = int((now - self._offline_since) // 60)
        self._file_for_owner(
            f"⚠️ {self.cfg.agent_name} has been unable to reach the concierge for {minutes} min "
            f"({reason}); the room cannot see it. Still retrying.",
            what="outage",
        )
        self._notify([{"kind": "delegate_offline", "at": now, "since": self._offline_since, "reason": reason}])

    def _offline_over(self) -> None:
        """Every successful hello lands here; closes a reported outage."""
        if self._offline_since is not None and self._offline_alerted:
            now = self.clock()
            hours, rest = divmod(int(now - self._offline_since), 3600)
            self._file_for_owner(
                f"✅ {self.cfg.agent_name} is back in the room after {hours} h {rest // 60} min offline.",
                what="return",
            )
            self._notify([{"kind": "delegate_back", "at": now, "since": self._offline_since}])
        self._offline_since = None
        self._offline_alerted = False

    def _process_outbox(self) -> None:
        """Act on the owner's notes: the channel INTO the room for the owner
        or their main agent. One note = one room message (or [SHARE]); notes
        that cannot go out yet (no quota, adapter down) stay in the outbox
        and are retried next cycle. Processed notes move to ``outbox/sent/``.
        """
        d = self.cfg.outbox_dir
        if not d.is_dir():
            return
        notes = sorted(
            p for p in d.iterdir()
            if p.is_file() and not p.name.startswith(".") and p.suffix in (".md", ".txt")
        )
        if not notes:
            return
        # Cheap gate BEFORE composing: a rejected note used to cost an LLM
        # call per retry, forever, while waiting_for_humans was on (found by
        # Sebastián, 26-aug-2026). Ask the concierge first, and back off —
        # the backoff lifts the moment a human speaks (see _process_batch).
        if self.clock() < self._outbox_backoff_until:
            return
        room = self.client.room()
        self.room = room
        remaining = int(room.get("you", {}).get("spontaneous_remaining_24h", 0))
        # Owner notes are the owner speaking (origin_owner): no quota applies.
        # Only a running ritual makes them wait.
        if room.get("ritual"):
            log.info("%d owner note(s) wait: ritual running", len(notes))
            self._outbox_backoff_until = self.clock() + 300
            return
        for path in notes:
            note = path.read_text(encoding="utf-8").strip()
            if note:
                prompt = build_owner_note_prompt(
                    transcript=self._transcript(), note=note,
                    owner_name=self.cfg.owner_name, remaining=remaining,
                )
                text = self._complete_or_fallback(prompt)
                if text is None:
                    log.error("adapter failed on owner note %s; will retry next cycle", path.name)
                    return
                if text.strip() and text.strip() != SILENCE:
                    try:
                        sent = self._say_or_share(self._clip(text), origin_owner=True)
                        log.info("owner note %s -> room: %s", path.name, sent[:80].replace("\n", " "))
                    except ConciergeError as exc:
                        log.warning("owner note %s rejected by concierge (%s); backing off", path.name, exc)
                        if exc.status == 429:
                            self._outbox_backoff_until = self.clock() + 300
                        return
                else:
                    log.info("owner note %s needed no message", path.name)
            sent_dir = d / "sent"
            sent_dir.mkdir(exist_ok=True)
            path.rename(sent_dir / f"{int(self.clock())}-{path.name}")

    def _refresh_room_map(self) -> None:
        """Rituals update the room map on close, but a long spontaneous
        conversation never did — commitments made there (dates, data owed)
        were forgotten. Refresh the notes every N room messages instead.
        """
        prompt = build_memory_prompt(
            current_notes=self._read(self.room_map_path),
            ritual_transcript=self._transcript(limit=60),
            instruction=(
                "Update your notes with what happened in the recent conversation: "
                "commitments and their dates, data people owe or expect, and new "
                "facts about the people. Keep what is still true; drop what is stale."
            ),
        )
        text = self._complete(prompt)
        if text and text.strip() and text.strip() != SILENCE:
            self.room_map_path.parent.mkdir(parents=True, exist_ok=True)
            self.room_map_path.write_text(text.strip() + "\n", encoding="utf-8")
            log.info("room map refreshed after %s message(s)", self._state.get("msgs_since_map"))
        self._state["msgs_since_map"] = 0

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
                self._state["msgs_since_map"] = 0
