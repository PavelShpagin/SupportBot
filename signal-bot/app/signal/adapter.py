from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Optional, List, Callable

from app.signal.signal_cli import InboundGroupMessage, InboundDirectMessage, GroupInfo


class SignalAdapter(Protocol):
    """Protocol for Signal communication adapters."""
    
    def send_group_text(
        self,
        *,
        group_id: str,
        text: str,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
    ) -> int | None: ...
    
    def send_direct_text(self, *, recipient: str, text: str) -> None: ...
    
    def send_direct_image(self, *, recipient: str, image_path: str, caption: str = "") -> None: ...
    
    def send_onboarding_prompt(self, *, recipient: str) -> None: ...
    
    def send_qr_for_group(self, *, recipient: str, group_name: str, qr_path: str) -> None: ...
    
    def send_success_message(self, *, recipient: str, group_name: str) -> None: ...
    
    def send_failure_message(self, *, recipient: str, group_name: str) -> None: ...
    
    def send_group_not_found(self, *, recipient: str) -> None: ...
    
    def list_groups(self) -> List[GroupInfo]: ...
    
    def find_group_by_name(self, name: str) -> Optional[GroupInfo]: ...


@dataclass(frozen=True)
class NoopSignalAdapter:
    """Noop adapter for local testing without Signal."""
    
    def send_group_text(
        self,
        *,
        group_id: str,
        text: str,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
    ) -> int | None:
        return None
    
    def send_direct_text(self, *, recipient: str, text: str) -> None:
        return
    
    def send_direct_image(self, *, recipient: str, image_path: str, caption: str = "") -> None:
        return
    
    def send_onboarding_prompt(self, *, recipient: str) -> None:
        return
    
    def send_qr_for_group(self, *, recipient: str, group_name: str, qr_path: str) -> None:
        return
    
    def send_success_message(self, *, recipient: str, group_name: str) -> None:
        return
    
    def send_failure_message(self, *, recipient: str, group_name: str) -> None:
        return
    
    def send_group_not_found(self, *, recipient: str) -> None:
        return
    
    def list_groups(self) -> List[GroupInfo]:
        return []
    
    def find_group_by_name(self, name: str) -> Optional[GroupInfo]:
        return None
