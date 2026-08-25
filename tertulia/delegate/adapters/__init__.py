"""Adapters turn (system prompt, prompt) into the agent's reply.

``claude_cli`` is the first one; the interface is small on purpose so other
runners (Codex, OpenClaw, a raw API call) can be added without touching the
daemon.
"""

from __future__ import annotations

from ..config import AdapterConfig
from .base import Adapter, AdapterError, Completion
from .claude_cli import ClaudeCliAdapter
from .scripted import ScriptedAdapter

__all__ = ["Adapter", "AdapterError", "Completion", "ClaudeCliAdapter", "ScriptedAdapter", "make_adapter"]


def make_adapter(cfg: AdapterConfig, *, sandbox_dir) -> Adapter:  # type: ignore[no-untyped-def]
    if cfg.kind == "claude_cli":
        return ClaudeCliAdapter(
            model=cfg.model,
            command=cfg.command,
            timeout_seconds=cfg.timeout_seconds,
            max_budget_usd=cfg.max_budget_usd,
            extra_args=list(cfg.extra_args),
            cwd=sandbox_dir,
        )
    if cfg.kind == "scripted":
        return ScriptedAdapter(cfg.responses)
    raise AdapterError(f"unknown adapter kind: {cfg.kind!r}")
