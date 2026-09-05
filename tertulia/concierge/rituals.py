"""Rituals: YAML specs + the deterministic state machine that runs them.

A ritual is a sequence of rounds. In each round every participant gets one
turn, one at a time, in a defined order: the concierge issues the turn (an
inbox event with the round's instruction and a deadline), waits for the
delegate's answer, and moves on — noting when a delegate is asleep or late.
The concierge never generates text: ``open`` and ``close`` are posted verbatim.

Placeholders available in ``open``/``close``: ``{{room}}``, ``{{newcomers}}``,
``{{participants}}``; in ``remind`` also ``{{when}}``.

Triggers: ``delegate_joined`` (the welcome), ``schedule`` (weekly, at
``schedule: "fri 18:00"`` in the room's timezone) and ``manual`` (only via
``/ritual <id>``).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Protocol
from zoneinfo import ZoneInfo

import yaml

log = logging.getLogger("tertulia.rituals")

PARTICIPANT_SETS = ("all", "newcomers", "others")
ORDERS = ("newcomers_first", "newcomers_last", "join_order")
AFTER_CLOSE_ACTIONS = ("update_memory",)
TRIGGERS = ("delegate_joined", "schedule", "manual")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class RitualSpecError(ValueError):
    pass


@dataclass(frozen=True)
class Schedule:
    """A weekly slot, ``"fri 18:00"``, read in the room's timezone."""

    weekday: int
    hour: int
    minute: int

    _RE = re.compile(r"\s*([a-z]{3})\s+(\d{1,2}):(\d{2})\s*")

    @classmethod
    def parse(cls, text: object, *, where: str = "schedule") -> "Schedule":
        m = cls._RE.fullmatch(str(text).lower())
        if not m or m.group(1) not in WEEKDAYS or int(m.group(2)) > 23 or int(m.group(3)) > 59:
            raise RitualSpecError(f"{where}: schedule must look like 'fri 18:00' (mon..sun, 24h time)")
        return cls(WEEKDAYS.index(m.group(1)), int(m.group(2)), int(m.group(3)))

    def _candidates(self, now: float, tz: ZoneInfo, days: range) -> Iterator[float]:
        base = datetime.fromtimestamp(now, tz)
        for delta in days:
            cand = (base + timedelta(days=delta)).replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
            if cand.weekday() == self.weekday:
                yield cand.timestamp()

    def at_or_before(self, now: float, tz: ZoneInfo) -> float:
        """The most recent occurrence at or before ``now``."""
        return max(t for t in self._candidates(now, tz, range(-7, 1)) if t <= now)

    def after(self, now: float, tz: ZoneInfo) -> float:
        """The next occurrence strictly after ``now``."""
        return min(t for t in self._candidates(now, tz, range(0, 8)) if t > now)


@dataclass(frozen=True)
class RoundSpec:
    id: str
    instruction: str
    participants: str = "all"
    order: str = "newcomers_first"
    timeout_seconds: int = 180


@dataclass(frozen=True)
class AfterCloseAction:
    action: str
    instruction: str


@dataclass(frozen=True)
class RitualSpec:
    id: str
    name: str
    trigger: str
    open: str
    close: str
    rounds: tuple[RoundSpec, ...]
    after_close: tuple[AfterCloseAction, ...] = field(default_factory=tuple)
    # trigger=schedule only: when it runs, and the heads-up posted this many
    # minutes before (0 = none) so owners can brief their delegates.
    schedule: Schedule | None = None
    remind_before_minutes: int = 0
    remind: str = ""


def _require(raw: dict[str, Any], key: str, where: str) -> Any:
    if key not in raw or raw[key] in (None, ""):
        raise RitualSpecError(f"{where}: missing '{key}'")
    return raw[key]


def parse_ritual(raw: dict[str, Any], *, where: str = "ritual") -> RitualSpec:
    rounds: list[RoundSpec] = []
    for i, r in enumerate(_require(raw, "rounds", where)):
        rw = f"{where}.rounds[{i}]"
        spec = RoundSpec(
            id=str(_require(r, "id", rw)),
            instruction=str(_require(r, "instruction", rw)).strip(),
            participants=str(r.get("participants", "all")),
            order=str(r.get("order", "newcomers_first")),
            timeout_seconds=int(r.get("timeout_seconds", 180)),
        )
        if spec.participants not in PARTICIPANT_SETS:
            raise RitualSpecError(f"{rw}: participants must be one of {PARTICIPANT_SETS}")
        if spec.order not in ORDERS:
            raise RitualSpecError(f"{rw}: order must be one of {ORDERS}")
        rounds.append(spec)
    if not rounds:
        raise RitualSpecError(f"{where}: a ritual needs at least one round")
    after: list[AfterCloseAction] = []
    for i, a in enumerate(raw.get("after_close") or []):
        aw = f"{where}.after_close[{i}]"
        action = AfterCloseAction(action=str(_require(a, "action", aw)), instruction=str(_require(a, "instruction", aw)).strip())
        if action.action not in AFTER_CLOSE_ACTIONS:
            raise RitualSpecError(f"{aw}: action must be one of {AFTER_CLOSE_ACTIONS}")
        after.append(action)
    trigger = str(raw.get("trigger", "manual"))
    if trigger not in TRIGGERS:
        raise RitualSpecError(f"{where}: trigger must be one of {TRIGGERS}")
    schedule = Schedule.parse(raw["schedule"], where=f"{where}.schedule") if raw.get("schedule") else None
    if trigger == "schedule" and schedule is None:
        raise RitualSpecError(f"{where}: trigger 'schedule' needs a schedule (e.g. \"fri 18:00\")")
    return RitualSpec(
        id=str(_require(raw, "id", where)),
        name=str(raw.get("name") or raw["id"]),
        trigger=trigger,
        open=str(_require(raw, "open", where)).strip(),
        close=str(_require(raw, "close", where)).strip(),
        rounds=tuple(rounds),
        after_close=tuple(after),
        schedule=schedule,
        remind_before_minutes=int(raw.get("remind_before_minutes") or 0),
        remind=str(raw.get("remind") or "").strip(),
    )


def load_ritual(path: Path) -> RitualSpec:
    with Path(path).open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise RitualSpecError(f"{path}: ritual file must be a mapping")
    return parse_ritual(raw, where=str(path))


def load_rituals(directory: Path) -> dict[str, RitualSpec]:
    """Load every ``*.yaml`` in ``directory``, keyed by ritual id."""
    specs: dict[str, RitualSpec] = {}
    for path in sorted(Path(directory).glob("*.yaml")):
        spec = load_ritual(path)
        if spec.id in specs:
            raise RitualSpecError(f"duplicate ritual id '{spec.id}' in {path}")
        specs[spec.id] = spec
    return specs


class RoomPort(Protocol):
    """What the ritual state machine needs from the concierge."""

    def post_concierge(self, text: str, *, ritual_id: int) -> None: ...
    def broadcast(self, kind: str, payload: dict[str, Any]) -> None: ...
    def issue_turn(self, *, ritual_id: int, ritual: str, round_id: str, delegate_id: int, instruction: str, deadline: float) -> int: ...
    def turn_status(self, turn_id: int) -> str: ...
    def close_turn(self, turn_id: int, status: str) -> None: ...
    def delegate_online(self, delegate_id: int) -> bool: ...
    def delegate_label(self, delegate_id: int) -> str: ...
    def note(self, key: str, **fmt: object) -> str: ...
    def join_names(self, names: list[str]) -> str: ...
    def room_name(self) -> str: ...


_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render(template: str, **values: str) -> str:
    """Fill ``{{name}}`` placeholders; unknown ones are left as they are."""
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template)


class RitualRun:
    """One running ritual. Drive it with ``tick`` from the concierge's loop and
    feed it answers with ``on_answer``; it is done when ``phase == 'done'``."""

    def __init__(self, *, ritual_id: int, spec: RitualSpec, participants: list[int], newcomers: list[int]):
        self.ritual_id = ritual_id
        self.spec = spec
        self.participants = list(participants)  # delegate ids in join order
        self.newcomers = [d for d in newcomers if d in self.participants]
        self.phase = "open"  # open -> rounds -> close -> done
        self.round_index = -1
        self.queue: list[int] = []
        self.current: dict[str, Any] | None = None  # {"turn_id", "delegate_id", "deadline"}

    # -- persistence ----------------------------------------------------------

    def to_state(self) -> dict[str, Any]:
        return {
            "spec_id": self.spec.id,
            "participants": self.participants,
            "newcomers": self.newcomers,
            "phase": self.phase,
            "round_index": self.round_index,
            "queue": self.queue,
            "current": self.current,
        }

    # -- helpers --------------------------------------------------------------

    @property
    def current_round(self) -> RoundSpec | None:
        if 0 <= self.round_index < len(self.spec.rounds):
            return self.spec.rounds[self.round_index]
        return None

    def _round_queue(self, rnd: RoundSpec) -> list[int]:
        others = [d for d in self.participants if d not in self.newcomers]
        if rnd.participants == "newcomers":
            return list(self.newcomers)
        if rnd.participants == "others":
            return others
        if rnd.order == "newcomers_first":
            return list(self.newcomers) + others
        if rnd.order == "newcomers_last":
            return others + list(self.newcomers)
        return list(self.participants)

    def _render(self, template: str, port: RoomPort) -> str:
        return render(
            template,
            room=port.room_name(),
            newcomers=port.join_names([port.delegate_label(d) for d in self.newcomers]),
            participants=port.join_names([port.delegate_label(d) for d in self.participants]),
        )

    def _next_round(self) -> bool:
        self.round_index += 1
        rnd = self.current_round
        if rnd is None:
            return False
        self.queue = self._round_queue(rnd)
        return True

    # -- driving --------------------------------------------------------------

    def tick(self, now: float, port: RoomPort) -> bool:
        """Advance as far as possible without waiting. Returns True when done."""
        if self.phase == "open":
            port.post_concierge(self._render(self.spec.open, port), ritual_id=self.ritual_id)
            port.broadcast("ritual_opened", {"ritual_id": self.ritual_id, "ritual": self.spec.id})
            self.phase = "rounds"
            self._next_round()
        if self.phase == "rounds":
            self._advance(now, port)
        if self.phase == "close":
            port.post_concierge(self._render(self.spec.close, port), ritual_id=self.ritual_id)
            port.broadcast(
                "ritual_closed",
                {
                    "ritual_id": self.ritual_id,
                    "ritual": self.spec.id,
                    "after_close": [{"action": a.action, "instruction": a.instruction} for a in self.spec.after_close],
                },
            )
            self.phase = "done"
        return self.phase == "done"

    def _advance(self, now: float, port: RoomPort) -> None:
        while True:
            if self.current is not None:
                if now < self.current["deadline"] or port.turn_status(self.current["turn_id"]) == "answering":
                    return  # still waiting
                port.close_turn(self.current["turn_id"], "expired")
                port.post_concierge(
                    port.note("turn_timeout", label=port.delegate_label(self.current["delegate_id"])),
                    ritual_id=self.ritual_id,
                )
                self.current = None
            if not self.queue:
                if not self._next_round():
                    self.phase = "close"
                    return
                continue
            rnd = self.current_round
            assert rnd is not None
            delegate_id = self.queue.pop(0)
            if not port.delegate_online(delegate_id):
                port.post_concierge(port.note("asleep", label=port.delegate_label(delegate_id)), ritual_id=self.ritual_id)
                continue
            deadline = now + rnd.timeout_seconds
            turn_id = port.issue_turn(
                ritual_id=self.ritual_id,
                ritual=self.spec.id,
                round_id=rnd.id,
                delegate_id=delegate_id,
                instruction=rnd.instruction,
                deadline=deadline,
            )
            self.current = {"turn_id": turn_id, "delegate_id": delegate_id, "deadline": deadline}
            return

    def on_answer(self, turn_id: int) -> bool:
        """Called when the delegate holding ``turn_id`` answered or passed."""
        if self.current is not None and self.current["turn_id"] == turn_id:
            self.current = None
            return True
        return False
