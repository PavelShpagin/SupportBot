"""
Signal Desktop Adapter for signal-bot.

This adapter implements the SignalAdapter protocol using Signal Desktop
via HTTP API (which in turn uses Chrome DevTools Protocol).

This replaces signal-cli for both sending and receiving messages.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import httpx

from app.config import Settings
from app.signal.signal_cli import (
    InboundGroupMessage,
    InboundDirectMessage,
    InboundReaction,
    GroupInfo,
    # Import message templates
    ONBOARDING_PROMPT_UK,
    ONBOARDING_PROMPT_EN,
    QR_MESSAGE_UK,
    QR_MESSAGE_EN,
    SUCCESS_MESSAGE_UK,
    SUCCESS_MESSAGE_EN,
    FAILURE_MESSAGE_UK,
    FAILURE_MESSAGE_EN,
    GROUP_NOT_FOUND_UK,
    GROUP_NOT_FOUND_EN,
    SEARCHING_GROUP_UK,
    SEARCHING_GROUP_EN,
    PROCESSING_MESSAGE_UK,
    PROCESSING_MESSAGE_EN,
    LANG_CHANGED_UK,
    LANG_CHANGED_EN,
    _msg,
)

log = logging.getLogger(__name__)


@dataclass
class SignalDesktopAdapter:
    """
    Signal adapter that uses Signal Desktop via HTTP API.
    
    This adapter communicates with the signal-desktop service which runs
    Signal Desktop headlessly and provides HTTP endpoints for:
    - Sending messages (via DevTools)
    - Receiving messages (via SQLite polling)
    - Listing groups and conversations
    """
    settings: Settings
    desktop_url: str = ""
    _poll_interval: float = 1.0
    _last_poll_ts: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    
    def __post_init__(self):
        # Use the signal desktop URL from settings or default
        if not self.desktop_url:
            self.desktop_url = getattr(self.settings, 'signal_desktop_url', 'http://signal-desktop-arm64:8001')
        self.desktop_url = self.desktop_url.rstrip('/')
    
    def _client(self) -> httpx.Client:
        """Create an HTTP client for the signal-desktop service."""
        return httpx.Client(base_url=self.desktop_url, timeout=60)
    
    def assert_available(self) -> None:
        """Check that signal-desktop service is available."""
        try:
            with self._client() as client:
                resp = client.get("/healthz")
                resp.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"Signal Desktop service not available at {self.desktop_url}: {e}")
    
    def is_linked(self) -> bool:
        """Check if Signal Desktop is linked to an account."""
        try:
            with self._client() as client:
                resp = client.get("/status")
                resp.raise_for_status()
                data = resp.json()
                return data.get("linked", False)
        except Exception as e:
            log.warning("Failed to check Signal Desktop status: %s", e)
            return False
    
    def is_devtools_connected(self) -> bool:
        """Check if DevTools is connected to Signal Desktop."""
        try:
            with self._client() as client:
                resp = client.get("/devtools/status")
                resp.raise_for_status()
                data = resp.json()
                return data.get("connected", False)
        except Exception as e:
            log.warning("Failed to check DevTools status: %s", e)
            return False
    
    def connect_devtools(self) -> bool:
        """Connect DevTools to Signal Desktop."""
        try:
            with self._client() as client:
                resp = client.post("/devtools/connect")
                resp.raise_for_status()
                data = resp.json()
                return data.get("connected", False)
        except Exception as e:
            log.warning("Failed to connect DevTools: %s", e)
            return False
    
    # ─────────────────────────────────────────────────────────────────────────
    # Send methods
    # ─────────────────────────────────────────────────────────────────────────
    
    def send_group_text(
        self,
        *,
        group_id: str,
        text: str,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
        mention_recipients: List[str] | None = None,
    ) -> None:
        """Send text message to a group."""
        with self._lock:
            try:
                # Note: Signal Desktop API doesn't support quotes/mentions yet
                # TODO: Add quote/mention support to DevTools client
                with self._client() as client:
                    resp = client.post("/send/group", json={
                        "group_id": group_id,
                        "text": text,
                        "expire_timer": 0,
                    })
                    resp.raise_for_status()
                    result = resp.json()
                    if not result.get("success"):
                        raise RuntimeError(f"Failed to send group message: {result}")
                    log.info("Sent message to group %s via Signal Desktop", group_id)
            except Exception as e:
                log.exception("Failed to send group message via Signal Desktop")
                raise RuntimeError(f"Signal Desktop send failed: {e}")
    
    def send_direct_text(self, *, recipient: str, text: str) -> bool:
        """Send text message to a user (1:1 chat).
        
        Returns True if sent successfully, False if user appears to have blocked/removed us.
        """
        with self._lock:
            try:
                with self._client() as client:
                    resp = client.post("/send", json={
                        "recipient": recipient,
                        "text": text,
                        "expire_timer": 0,
                    })
                    resp.raise_for_status()
                    result = resp.json()
                    if result.get("success"):
                        log.info("Sent direct message to %s via Signal Desktop", recipient)
                        return True
                    else:
                        log.warning("Failed to send direct message: %s", result)
                        return False
            except Exception as e:
                log.exception("Failed to send direct message via Signal Desktop")
                return False
    
    def send_direct_image(self, *, recipient: str, image_path: str, caption: str = "") -> None:
        """Send image to a user (1:1 chat) with optional caption."""
        with self._lock:
            try:
                with self._client() as client:
                    resp = client.post("/send/image", json={
                        "recipient": recipient,
                        "image_path": image_path,
                        "caption": caption,
                    })
                    if resp.status_code == 501:
                        log.warning("Image sending not yet implemented in Signal Desktop, falling back to text")
                        # Fallback: send caption as text
                        if caption:
                            self.send_direct_text(recipient=recipient, text=caption)
                        return
                    resp.raise_for_status()
                    log.info("Sent image to %s via Signal Desktop", recipient)
            except Exception as e:
                log.exception("Failed to send image via Signal Desktop")
                raise RuntimeError(f"Signal Desktop send image failed: {e}")
    
    # ─────────────────────────────────────────────────────────────────────────
    # Admin onboarding messages (with language support)
    # ─────────────────────────────────────────────────────────────────────────
    
    def send_onboarding_prompt(self, *, recipient: str, lang: str = "uk") -> bool:
        """Send the initial onboarding message asking for group name."""
        text = _msg(ONBOARDING_PROMPT_UK, ONBOARDING_PROMPT_EN, lang)
        try:
            result = self.send_direct_text(recipient=recipient, text=text)
            if result:
                log.info("Sent onboarding prompt to %s (lang=%s)", recipient, lang)
            return result
        except Exception:
            log.exception("Failed to send onboarding prompt to %s", recipient)
            return False
    
    def send_qr_for_group(self, *, recipient: str, group_name: str, qr_path: str, lang: str = "uk") -> None:
        """Send QR code image with explanation to admin."""
        caption = _msg(
            QR_MESSAGE_UK.format(group_name=group_name),
            QR_MESSAGE_EN.format(group_name=group_name),
            lang
        )
        try:
            self.send_direct_image(recipient=recipient, image_path=qr_path, caption=caption)
            log.info("Sent QR for group %s to %s (lang=%s)", group_name, recipient, lang)
        except Exception:
            log.exception("Failed to send QR to %s", recipient)
    
    def send_success_message(self, *, recipient: str, group_name: str, lang: str = "uk") -> None:
        """Send success confirmation after QR scan."""
        text = _msg(
            SUCCESS_MESSAGE_UK.format(group_name=group_name),
            SUCCESS_MESSAGE_EN.format(group_name=group_name),
            lang
        )
        try:
            self.send_direct_text(recipient=recipient, text=text)
            log.info("Sent success message to %s for group %s (lang=%s)", recipient, group_name, lang)
        except Exception:
            log.exception("Failed to send success message to %s", recipient)
    
    def send_failure_message(self, *, recipient: str, group_name: str, lang: str = "uk") -> None:
        """Send failure message if QR scan failed."""
        text = _msg(
            FAILURE_MESSAGE_UK.format(group_name=group_name),
            FAILURE_MESSAGE_EN.format(group_name=group_name),
            lang
        )
        try:
            self.send_direct_text(recipient=recipient, text=text)
            log.info("Sent failure message to %s for group %s (lang=%s)", recipient, group_name, lang)
        except Exception:
            log.exception("Failed to send failure message to %s", recipient)
    
    def send_group_not_found(self, *, recipient: str, lang: str = "uk") -> None:
        """Send message when group name doesn't match any known group."""
        text = _msg(GROUP_NOT_FOUND_UK, GROUP_NOT_FOUND_EN, lang)
        try:
            self.send_direct_text(recipient=recipient, text=text)
            log.info("Sent group not found message to %s (lang=%s)", recipient, lang)
        except Exception:
            log.exception("Failed to send group not found message to %s", recipient)
    
    def send_processing_message(self, *, recipient: str, group_name: str, lang: str = "uk") -> None:
        """Send message when group is found and QR is being generated."""
        text = _msg(
            PROCESSING_MESSAGE_UK.format(group_name=group_name),
            PROCESSING_MESSAGE_EN.format(group_name=group_name),
            lang
        )
        try:
            self.send_direct_text(recipient=recipient, text=text)
            log.info("Sent processing message to %s for group %s (lang=%s)", recipient, group_name, lang)
        except Exception:
            log.exception("Failed to send processing message to %s", recipient)
    
    def send_searching_message(self, *, recipient: str, group_name: str, lang: str = "uk") -> None:
        """Send instant feedback when user sends group name."""
        text = _msg(
            SEARCHING_GROUP_UK.format(group_name=group_name),
            SEARCHING_GROUP_EN.format(group_name=group_name),
            lang
        )
        try:
            self.send_direct_text(recipient=recipient, text=text)
            log.info("Sent searching message to %s for group %s (lang=%s)", recipient, group_name, lang)
        except Exception:
            log.exception("Failed to send searching message to %s", recipient)
    
    def send_lang_changed(self, *, recipient: str, lang: str) -> None:
        """Send language change confirmation."""
        text = _msg(LANG_CHANGED_UK, LANG_CHANGED_EN, lang)
        try:
            self.send_direct_text(recipient=recipient, text=text)
            log.info("Sent lang changed message to %s (lang=%s)", recipient, lang)
        except Exception:
            log.exception("Failed to send lang changed message to %s", recipient)
    
    # ─────────────────────────────────────────────────────────────────────────
    # List groups
    # ─────────────────────────────────────────────────────────────────────────
    
    def list_groups(self) -> List[GroupInfo]:
        """List all groups the bot is a member of."""
        try:
            with self._client() as client:
                resp = client.get("/groups")
                resp.raise_for_status()
                data = resp.json()
                groups = []
                for g in data.get("groups", []):
                    groups.append(GroupInfo(
                        group_id=g.get("group_id") or g.get("id", ""),
                        group_name=g.get("name", ""),
                    ))
                return groups
        except Exception as e:
            log.warning("Failed to list groups via Signal Desktop: %s", e)
            return []
    
    def find_group_by_name(self, name: str) -> Optional[GroupInfo]:
        """Find a group by name (case-insensitive partial match)."""
        try:
            with self._client() as client:
                resp = client.get("/groups/find", params={"name": name})
                resp.raise_for_status()
                data = resp.json()
                if data.get("found") and data.get("group"):
                    g = data["group"]
                    return GroupInfo(
                        group_id=g.get("group_id") or g.get("id", ""),
                        group_name=g.get("name", ""),
                    )
                return None
        except Exception as e:
            log.warning("Failed to find group via Signal Desktop: %s", e)
            return None

    def list_contacts(self) -> Optional[set[str]]:
        """List contact identifiers from DB for pruning when user removes bot."""
        try:
            with self._client() as client:
                resp = client.get("/contacts")
                resp.raise_for_status()
                data = resp.json()
                return set(data.get("contacts", []))
        except Exception as e:
            log.warning("Failed to list contacts via Signal Desktop: %s", e)
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # Listen loop (polls Signal Desktop for new messages)
    # ─────────────────────────────────────────────────────────────────────────
    
    def listen_forever(
        self,
        *,
        on_group_message: Callable[[InboundGroupMessage], None],
        on_direct_message: Callable[[InboundDirectMessage], None],
        on_reaction: Callable[[InboundReaction], None] | None = None,
        on_contact_removed: Callable[[str], None] | None = None,
    ) -> None:
        """
        Signal receive loop. Polls Signal Desktop for new messages and dispatches:
        - Group messages -> on_group_message
        - Direct (1:1) messages -> on_direct_message
        """
        log.info("Starting Signal Desktop receive loop...")
        
        # Ensure DevTools is connected
        if not self.is_devtools_connected():
            log.info("Connecting DevTools...")
            self.connect_devtools()
        
        while True:
            try:
                with self._client() as client:
                    resp = client.get("/messages", params={
                        "since_ts": self._last_poll_ts,
                        "limit": 100,
                    })
                    resp.raise_for_status()
                    data = resp.json()
                    
                    messages = data.get("messages", [])
                    if messages:
                        log.debug("Received %d messages from Signal Desktop", len(messages))
                    
                    for msg in messages:
                        try:
                            # Update last poll timestamp
                            ts = msg.get("timestamp", 0)
                            if ts > self._last_poll_ts:
                                self._last_poll_ts = ts
                            
                            # Parse message
                            group_id = msg.get("group_id")
                            msg_type = msg.get("type", "")
                            
                            # Skip outgoing messages
                            if msg_type == "outgoing":
                                continue
                            
                            if group_id:
                                # Group message
                                group_msg = InboundGroupMessage(
                                    message_id=str(msg.get("id", "")),
                                    group_id=str(group_id),
                                    sender=str(msg.get("sender", "")),
                                    ts=int(ts),
                                    text=str(msg.get("body", "")),
                                    image_paths=[],  # TODO: handle attachments
                                    reply_to_id=None,
                                )
                                try:
                                    on_group_message(group_msg)
                                except Exception:
                                    log.exception("on_group_message handler failed")
                            else:
                                # Direct message
                                direct_msg = InboundDirectMessage(
                                    message_id=str(msg.get("id", "")),
                                    sender=str(msg.get("sender", "")),
                                    ts=int(ts),
                                    text=str(msg.get("body", "")),
                                    image_paths=[],  # TODO: handle attachments
                                )
                                try:
                                    on_direct_message(direct_msg)
                                except Exception:
                                    log.exception("on_direct_message handler failed")
                                    
                        except Exception as e:
                            log.exception("Failed to process message: %s", msg)
                    
                    # Update last_ts from response
                    if data.get("last_ts"):
                        self._last_poll_ts = max(self._last_poll_ts, data["last_ts"])
                        
            except Exception as e:
                log.warning("Signal Desktop poll failed: %s; retrying in 5s", e)
                time.sleep(5)
                continue
            
            # Poll interval
            time.sleep(self._poll_interval)
