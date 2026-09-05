"""Delegate configuration (``delegate.yaml`` next to ``profile.md``).

Paths are resolved relative to the config file. The delegate token is read
from ``TERTULIA_DELEGATE_TOKEN`` if set, otherwise from ``token_file``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


@dataclass
class AdapterConfig:
    kind: str = "claude_cli"
    # The voice: writes every message the delegate posts (and its room map).
    # Needs a plan/key that includes it; drop to sonnet if yours does not.
    model: str = "opus"
    # Cheap triage for routine decisions ("is this worth answering?").
    # Set to null to run everything through `model`.
    fast_model: str | None = "haiku"
    command: str = "claude"
    # Wall-clock cap per model call. The voice on a long transcript can take
    # minutes; a tight cap fails the call, then the fallback, then the whole
    # cycle (27-aug-2026: one owner note, 5 cycles, 20 min, 10 dead calls).
    timeout_seconds: int = 300
    # Per-call cap: a runaway guard, NOT cost control. On subscription plans
    # the reported cost is bookkeeping, and 0.10 already aborted a legitimate
    # reply mid-call (26-aug-2026) — keep this far above any real call.
    max_budget_usd: float = 1.00
    extra_args: list[str] = field(default_factory=list)
    # Only for kind=scripted (tests/dry runs): canned replies, cycled.
    responses: list[str] = field(default_factory=list)


@dataclass
class BehaviourConfig:
    # Consider replying (the agent may still stay silent) to human messages.
    react_to_humans: bool = True
    # Outside rituals, react to other delegates only when addressed by name.
    react_to_delegates: bool = False
    # Ignore messages older than this when waking up (no replying to yesterday).
    react_max_age_seconds: int = 600
    # How many recent room messages to show the agent as context.
    transcript_window: int = 40
    # After the first event of a batch, wait this long to gather the rest.
    batch_settle_seconds: float = 2.0
    # Refresh the room map after this many room messages outside rituals —
    # commitments made in spontaneous conversation must not be forgotten.
    # 0 disables (rituals still update the map on close).
    map_update_every_messages: int = 20
    # Unable to reach the concierge for this long: tell the owner once
    # (for-owner.md + notify_command) and again when back. 0 disables.
    # (4-sep-2026: eleven hours behind a web filter, retrying every minute,
    # and the owner learned it from the log the next day.)
    offline_alert_seconds: int = 900


@dataclass
class DelegateConfig:
    concierge_url: str
    agent_name: str
    owner_name: str
    personality: str
    profile_path: Path
    memory_dir: Path
    state_dir: Path
    sandbox_dir: Path
    # The pre-approved catalogue: the owner approves a file for the room by
    # placing it here; the delegate can share nothing else.
    shared_dir: Path
    # The owner's channel INTO the room: drop a .md/.txt note here and the
    # delegate acts on it in the room (the owner's main agent can write too).
    outbox_dir: Path
    token_file: Path
    owner_telegram_user_id: int | None
    adapter: AdapterConfig
    behaviour: BehaviourConfig
    base_dir: Path
    # Optional shell command run on every inbox batch with the events as JSON
    # on stdin (fire-and-forget, cwd = the delegate folder). Lets the owner
    # observe the room from outside the delegate.
    notify_command: str | None = None
    # The owner's material for a scheduled ritual: ``<ritual>.md`` dropped
    # here (e.g. ``weekly.md``) is what the delegate speaks from in that
    # ritual's turns; archived to ``sent/`` when the ritual closes.
    briefing_dir: Path | None = None
    # On `run`, best-effort `git pull --ff-only` of the checkout this code runs
    # from, restarting on new code. OFF unless the member opts in: the daemon is
    # not the sandboxed part (that is `claude -p`), so auto-pulling means the
    # repo owner can run code on your machine — consent must be explicit.
    auto_update: bool = False

    def token(self) -> str:
        env = os.environ.get("TERTULIA_DELEGATE_TOKEN", "").strip()
        if env:
            return env
        if self.token_file.exists():
            token = self.token_file.read_text(encoding="utf-8").strip()
            if token:
                return token
        raise ConfigError(
            f"delegate token not found: set TERTULIA_DELEGATE_TOKEN or write it to {self.token_file}"
        )


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"'{key}' must be a mapping")
    return value


def load_config(path: str | Path) -> DelegateConfig:
    path = Path(path).expanduser().resolve()
    base = path.parent
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    for key in ("concierge_url", "agent_name", "owner_name"):
        if not raw.get(key):
            raise ConfigError(f"'{key}' is required")

    adapter_raw = dict(_section(raw, "adapter"))
    adapter = AdapterConfig(**adapter_raw)
    behaviour = BehaviourConfig(**_section(raw, "behaviour"))

    def rel(key: str, default: str) -> Path:
        return (base / Path(str(raw.get(key) or default))).resolve()

    owner_id = raw.get("owner_telegram_user_id")
    return DelegateConfig(
        concierge_url=str(raw["concierge_url"]).rstrip("/"),
        agent_name=str(raw["agent_name"]).strip(),
        owner_name=str(raw["owner_name"]).strip(),
        personality=str(raw.get("personality") or "").strip(),
        profile_path=rel("profile", "profile.md"),
        memory_dir=rel("memory_dir", "memory"),
        state_dir=rel("state_dir", "state"),
        sandbox_dir=rel("sandbox_dir", "sandbox"),
        shared_dir=rel("shared_dir", "shared"),
        outbox_dir=rel("outbox_dir", "outbox"),
        token_file=rel("token_file", "token"),
        owner_telegram_user_id=int(owner_id) if owner_id else None,
        adapter=adapter,
        behaviour=behaviour,
        base_dir=base,
        notify_command=str(raw["notify_command"]) if raw.get("notify_command") else None,
        briefing_dir=rel("briefing_dir", "briefing"),
        auto_update=bool(raw.get("auto_update", False)),
    )
