"""Brakes for spontaneous (non-ritual) delegate messages.

Pure functions over a small snapshot so the rules are trivial to test. The
concierge applies them to every ``say`` that is not answering a ritual turn.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    spontaneous_per_24h: int = 3
    min_gap_seconds: int = 30
    max_consecutive_delegate_messages: int = 4
    max_message_chars: int = 2000


@dataclass(frozen=True)
class SpontaneousSnapshot:
    sent_last_24h: int
    seconds_since_last_own: float | None
    consecutive_delegate_tail: int
    ritual_running: bool


def check_spontaneous(limits: Limits, snap: SpontaneousSnapshot) -> str | None:
    """Return a rejection reason, or None if the message may go out."""
    if snap.ritual_running:
        return "ritual_running"
    if snap.sent_last_24h >= limits.spontaneous_per_24h:
        return "daily_quota"
    if snap.seconds_since_last_own is not None and snap.seconds_since_last_own < limits.min_gap_seconds:
        return "too_soon"
    if snap.consecutive_delegate_tail >= limits.max_consecutive_delegate_messages:
        return "waiting_for_humans"
    return None


def check_text(limits: Limits, text: str) -> str | None:
    if not text.strip():
        return "empty"
    if len(text) > limits.max_message_chars:
        return "too_long"
    return None
