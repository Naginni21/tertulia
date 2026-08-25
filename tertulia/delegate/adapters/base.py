from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class AdapterError(Exception):
    pass


@dataclass
class Completion:
    text: str
    cost_usd: float | None = None
    raw: Any = None


class Adapter(Protocol):
    name: str

    def complete(
        self, *, system_prompt: str, prompt: str, timeout: float | None = None, model: str | None = None
    ) -> Completion:
        """Run one stateless completion, optionally overriding the model.
        Raise AdapterError on failure."""
        ...
