"""Adapter over the ``claude -p`` command line.

Every call is a fresh, stateless, tool-less session:

* ``--model`` is always explicit (cheap models for chat);
* ``--tools ""`` disables every built-in tool — the delegate cannot touch the
  machine; ``--safe-mode`` additionally drops hooks, CLAUDE.md, plugins and
  MCP servers of the host user;
* ``--strict-mcp-config --mcp-config '{"mcpServers":{}}'`` guarantees no
  inherited MCP servers;
* ``--no-session-persistence`` so nothing is written to the user's sessions;
* ``--max-budget-usd`` caps the damage of a runaway call.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from .base import AdapterError, Completion

log = logging.getLogger("tertulia.adapter.claude")

BASE_ARGS = [
    "-p",
    "--safe-mode",
    "--tools", "",
    "--strict-mcp-config",
    "--mcp-config", '{"mcpServers":{}}',
    "--no-session-persistence",
    "--disable-slash-commands",
    "--output-format", "json",
]


class ClaudeCliAdapter:
    name = "claude_cli"

    def __init__(
        self,
        *,
        model: str,
        command: str = "claude",
        timeout_seconds: int = 300,
        max_budget_usd: float = 0.10,
        extra_args: list[str] | None = None,
        cwd: Path | None = None,
    ):
        if not model:
            raise AdapterError("claude_cli adapter requires an explicit model (e.g. 'haiku' or 'sonnet')")
        resolved = shutil.which(command) or command
        self.command = resolved
        self.model = model
        self.timeout = timeout_seconds
        self.max_budget_usd = max_budget_usd
        self.extra_args = extra_args or []
        self.cwd = cwd

    def complete(
        self, *, system_prompt: str, prompt: str, timeout: float | None = None, model: str | None = None
    ) -> Completion:
        argv = [
            self.command,
            *BASE_ARGS,
            "--model", model or self.model,
            "--max-budget-usd", str(self.max_budget_usd),
            "--system-prompt", system_prompt,
            *self.extra_args,
        ]
        if self.cwd is not None:
            Path(self.cwd).mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run(
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                # text=True alone uses the locale encoding — cp1252 on Windows,
                # which dies on the emoji/arrows the room text carries.
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self.timeout,
                cwd=str(self.cwd) if self.cwd else None,
            )
        except FileNotFoundError:
            raise AdapterError(f"command not found: {self.command}") from None
        except subprocess.TimeoutExpired:
            raise AdapterError(f"claude timed out after {timeout or self.timeout}s") from None
        if proc.returncode != 0:
            raise AdapterError(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:400]}")
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            raise AdapterError(f"claude returned non-JSON output: {proc.stdout[:200]!r}") from None
        if data.get("is_error"):
            raise AdapterError(f"claude reported an error: {str(data.get('result'))[:400]}")
        text = str(data.get("result") or "").strip()
        cost = data.get("total_cost_usd")
        log.debug("claude %s: %.4f USD, %s ms", model or self.model, cost or 0.0, data.get("duration_ms"))
        return Completion(text=text, cost_usd=float(cost) if cost is not None else None, raw=data)
