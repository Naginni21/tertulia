"""A canned-reply adapter for tests and transport dry runs (no LLM, no cost)."""

from __future__ import annotations

import itertools
import threading
from typing import Callable, Iterable

from .base import Completion


class ScriptedAdapter:
    name = "scripted"

    def __init__(self, responses: Iterable[str] | Callable[[str, str], str]):
        self._calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        if callable(responses):
            self._fn = responses
        else:
            items = list(responses) or ["(scripted reply)"]
            cycle = itertools.cycle(items)
            self._fn = lambda _s, _p: next(cycle)

    @property
    def calls(self) -> list[tuple[str, str]]:
        return list(self._calls)

    def complete(self, *, system_prompt: str, prompt: str, timeout: float | None = None, model: str | None = None) -> Completion:
        with self._lock:
            self._calls.append((system_prompt, prompt))
            return Completion(text=self._fn(system_prompt, prompt), cost_usd=0.0)
