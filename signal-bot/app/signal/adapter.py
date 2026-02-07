from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class SignalAdapter(Protocol):
    def send_group_text(self, *, group_id: str, text: str) -> None: ...


@dataclass(frozen=True)
class NoopSignalAdapter:
    def send_group_text(self, *, group_id: str, text: str) -> None:
        # Useful for local testing without Signal.
        return

