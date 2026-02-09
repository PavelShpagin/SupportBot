from __future__ import annotations

import json
import logging
import threading
import time
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel, Field

from app.config import Settings

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Messages sent to admin in 1:1 chat (bilingual)
# ─────────────────────────────────────────────────────────────────────────────

ONBOARDING_PROMPT_UK = """Привіт! Я SupportBot — бот технічної підтримки.

Щоб підключити мене до групи:
1. Додайте мене до групи в Signal
2. Напишіть мені сюди назву групи

Детальніше: https://supportbot.info/

Яку групу ви хочете підключити?"""

ONBOARDING_PROMPT_EN = """Hi! I'm SupportBot — a technical support bot.

To connect me to a group:
1. Add me to a group in Signal
2. Send me the group name here

Learn more: https://supportbot.info/

Which group would you like to connect?"""

QR_MESSAGE_UK = """Відскануйте цей QR-код у Signal, щоб підтвердити доступ до групи "{group_name}".

Після сканування я зможу обробити історію групи та почати відповідати на питання.

Якщо ви хочете підключити іншу групу, просто напишіть її назву."""

QR_MESSAGE_EN = """Scan this QR code in Signal to confirm access to group "{group_name}".

After scanning, I'll be able to process the group history and start answering questions.

If you want to connect a different group, just send its name."""

SUCCESS_MESSAGE_UK = """Успішно підключено до групи "{group_name}"!

Я почну обробляти історію та формувати базу знань. Це може зайняти кілька хвилин.

Хочете підключити ще одну групу? Напишіть її назву."""

SUCCESS_MESSAGE_EN = """Successfully connected to group "{group_name}"!

I'll start processing the history and building the knowledge base. This may take a few minutes.

Want to connect another group? Send its name."""

FAILURE_MESSAGE_UK = """Не вдалося підключитися до групи "{group_name}".

Можливі причини:
• QR-код застарів (спробуйте ще раз)
• Виникла технічна помилка

Напишіть назву групи, щоб спробувати знову."""

FAILURE_MESSAGE_EN = """Failed to connect to group "{group_name}".

Possible reasons:
• QR code expired (try again)
• Technical error occurred

Send the group name to try again."""

GROUP_NOT_FOUND_UK = """Не знайшов групу з такою назвою. Переконайтеся, що:
1. Ви додали мене до цієї групи
2. Назва написана правильно

Спробуйте ще раз або напишіть іншу назву групи."""

GROUP_NOT_FOUND_EN = """Couldn't find a group with that name. Make sure:
1. You've added me to this group
2. The name is spelled correctly

Try again or send a different group name."""


# ─────────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────────

class InboundGroupMessage(BaseModel):
    """Message received in a group chat."""
    message_id: str
    group_id: str
    sender: str
    ts: int
    text: str = ""
    image_paths: list[str] = Field(default_factory=list)
    reply_to_id: str | None = None


class InboundDirectMessage(BaseModel):
    """Message received in 1:1 chat with admin."""
    message_id: str
    sender: str  # Admin's phone number or UUID
    ts: int
    text: str = ""
    image_paths: list[str] = Field(default_factory=list)


class GroupInfo(BaseModel):
    """Information about a group the bot is a member of."""
    group_id: str
    group_name: str


# ─────────────────────────────────────────────────────────────────────────────
# Signal CLI Adapter
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SignalCliAdapter:
    settings: Settings

    def _bin(self) -> str:
        return self.settings.signal_cli

    def _config(self) -> str:
        return self.settings.signal_bot_storage

    def _user(self) -> str:
        return self.settings.signal_bot_e164

    def assert_available(self) -> None:
        if shutil.which(self._bin()) is None:
            raise RuntimeError(f"signal-cli binary not found: {self._bin()}")

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
    ) -> None:
        """Send text message to a group."""
        self.assert_available()
        cmd = [
            self._bin(), "--config", self._config(), "-u", self._user(),
            "send", "-g", group_id, "-m", text,
        ]
        # Reply-to / quote support (signal-cli `send` flags).
        if quote_timestamp is not None:
            cmd.extend(["--quote-timestamp", str(int(quote_timestamp))])
        if quote_author:
            cmd.extend(["--quote-author", str(quote_author)])
        if quote_message:
            cmd.extend(["--quote-message", str(quote_message)])
        log.info("signal-cli send group_id=%s bytes=%s", group_id, len(text.encode("utf-8")))
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.stdout:
            log.info("signal-cli stdout: %s", proc.stdout.strip())
        if proc.stderr:
            log.info("signal-cli stderr: %s", proc.stderr.strip())
        if proc.returncode != 0:
            raise RuntimeError(f"signal-cli send failed (exit {proc.returncode})")

    def send_direct_text(self, *, recipient: str, text: str) -> None:
        """Send text message to a user (1:1 chat)."""
        self.assert_available()
        cmd = [
            self._bin(), "--config", self._config(), "-u", self._user(),
            "send", "-m", text, recipient,
        ]
        log.info("signal-cli send direct recipient=%s bytes=%s", recipient, len(text.encode("utf-8")))
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.stdout:
            log.info("signal-cli stdout: %s", proc.stdout.strip())
        if proc.stderr:
            log.info("signal-cli stderr: %s", proc.stderr.strip())
        if proc.returncode != 0:
            raise RuntimeError(f"signal-cli send failed (exit {proc.returncode})")

    def send_direct_image(self, *, recipient: str, image_path: str, caption: str = "") -> None:
        """Send image to a user (1:1 chat) with optional caption."""
        self.assert_available()
        cmd = [
            self._bin(), "--config", self._config(), "-u", self._user(),
            "send", "-a", image_path,
        ]
        if caption:
            cmd.extend(["-m", caption])
        cmd.append(recipient)
        log.info("signal-cli send image recipient=%s path=%s", recipient, image_path)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.stdout:
            log.info("signal-cli stdout: %s", proc.stdout.strip())
        if proc.stderr:
            log.info("signal-cli stderr: %s", proc.stderr.strip())
        if proc.returncode != 0:
            raise RuntimeError(f"signal-cli send image failed (exit {proc.returncode})")

    # ─────────────────────────────────────────────────────────────────────────
    # Admin onboarding messages
    # ─────────────────────────────────────────────────────────────────────────

    def send_onboarding_prompt(self, *, recipient: str) -> None:
        """Send the initial onboarding message asking for group name."""
        text = ONBOARDING_PROMPT_UK + "\n\n---\n\n" + ONBOARDING_PROMPT_EN
        try:
            self.send_direct_text(recipient=recipient, text=text)
            log.info("Sent onboarding prompt to %s", recipient)
        except Exception:
            log.exception("Failed to send onboarding prompt to %s", recipient)

    def send_qr_for_group(self, *, recipient: str, group_name: str, qr_path: str) -> None:
        """Send QR code image with explanation to admin."""
        caption = (
            QR_MESSAGE_UK.format(group_name=group_name) +
            "\n\n---\n\n" +
            QR_MESSAGE_EN.format(group_name=group_name)
        )
        try:
            self.send_direct_image(recipient=recipient, image_path=qr_path, caption=caption)
            log.info("Sent QR for group %s to %s", group_name, recipient)
        except Exception:
            log.exception("Failed to send QR to %s", recipient)

    def send_success_message(self, *, recipient: str, group_name: str) -> None:
        """Send success confirmation after QR scan."""
        text = (
            SUCCESS_MESSAGE_UK.format(group_name=group_name) +
            "\n\n---\n\n" +
            SUCCESS_MESSAGE_EN.format(group_name=group_name)
        )
        try:
            self.send_direct_text(recipient=recipient, text=text)
            log.info("Sent success message to %s for group %s", recipient, group_name)
        except Exception:
            log.exception("Failed to send success message to %s", recipient)

    def send_failure_message(self, *, recipient: str, group_name: str) -> None:
        """Send failure message if QR scan failed."""
        text = (
            FAILURE_MESSAGE_UK.format(group_name=group_name) +
            "\n\n---\n\n" +
            FAILURE_MESSAGE_EN.format(group_name=group_name)
        )
        try:
            self.send_direct_text(recipient=recipient, text=text)
            log.info("Sent failure message to %s for group %s", recipient, group_name)
        except Exception:
            log.exception("Failed to send failure message to %s", recipient)

    def send_group_not_found(self, *, recipient: str) -> None:
        """Send message when group name doesn't match any known group."""
        text = GROUP_NOT_FOUND_UK + "\n\n---\n\n" + GROUP_NOT_FOUND_EN
        try:
            self.send_direct_text(recipient=recipient, text=text)
            log.info("Sent group not found message to %s", recipient)
        except Exception:
            log.exception("Failed to send group not found message to %s", recipient)

    # ─────────────────────────────────────────────────────────────────────────
    # List groups
    # ─────────────────────────────────────────────────────────────────────────

    def list_groups(self) -> list[GroupInfo]:
        """List all groups the bot is a member of."""
        self.assert_available()
        cmd = [
            self._bin(), "--config", self._config(), "-u", self._user(),
            "listGroups", "-d", "--output", "json",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            log.warning("signal-cli listGroups failed: %s", proc.stderr)
            return []

        groups = []
        try:
            data = json.loads(proc.stdout) if proc.stdout.strip() else []
            if isinstance(data, list):
                for g in data:
                    if isinstance(g, dict):
                        gid = g.get("id") or g.get("groupId") or ""
                        gname = g.get("name") or g.get("groupName") or ""
                        if gid:
                            groups.append(GroupInfo(group_id=str(gid), group_name=str(gname)))
        except json.JSONDecodeError:
            log.warning("Failed to parse listGroups output")
        return groups

    def find_group_by_name(self, name: str) -> Optional[GroupInfo]:
        """Find a group by name (case-insensitive partial match)."""
        name_lower = name.strip().lower()
        if not name_lower:
            return None
        groups = self.list_groups()
        # Exact match first
        for g in groups:
            if g.group_name.lower() == name_lower:
                return g
        # Partial match
        for g in groups:
            if name_lower in g.group_name.lower():
                return g
        return None

    # ─────────────────────────────────────────────────────────────────────────
    # Listen loop
    # ─────────────────────────────────────────────────────────────────────────

    def listen_forever(
        self,
        *,
        on_group_message: Callable[[InboundGroupMessage], None],
        on_direct_message: Callable[[InboundDirectMessage], None],
    ) -> None:
        """
        Signal receive loop. Dispatches:
        - Group messages -> on_group_message
        - Direct (1:1) messages -> on_direct_message
        """
        self.assert_available()

        cmd = [
            self._bin(), "--config", self._config(), "-u", self._user(),
            "receive", "--output", "json",
        ]

        while True:
            log.info("Starting signal-cli receive loop")
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
            )

            def _drain_stderr() -> None:
                assert proc.stderr is not None
                for line in proc.stderr:
                    if line.strip():
                        log.info("signal-cli stderr: %s", line.strip())

            threading.Thread(target=_drain_stderr, daemon=True).start()

            assert proc.stdout is not None
            buf = ""
            for line in proc.stdout:
                if not line:
                    continue
                buf += line
                try:
                    obj = json.loads(buf)
                except json.JSONDecodeError:
                    continue

                buf = ""

                # Try parsing as group message
                group_msg = _parse_group_message(obj)
                if group_msg is not None:
                    try:
                        on_group_message(group_msg)
                    except Exception:
                        log.exception("on_group_message handler failed")
                    continue

                # Try parsing as direct message
                direct_msg = _parse_direct_message(obj)
                if direct_msg is not None:
                    try:
                        on_direct_message(direct_msg)
                    except Exception:
                        log.exception("on_direct_message handler failed")
                    continue

            rc = proc.wait(timeout=5)
            log.warning("signal-cli receive loop exited (rc=%s); restarting soon", rc)
            time.sleep(2)


# ─────────────────────────────────────────────────────────────────────────────
# Parsing helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_group_message(obj: dict) -> Optional[InboundGroupMessage]:
    """Parse a group message from signal-cli JSON."""
    env = obj.get("envelope") if isinstance(obj, dict) else None
    if not isinstance(env, dict):
        return None

    ts = env.get("timestamp")
    if ts is None:
        return None
    try:
        ts_i = int(ts)
    except Exception:
        return None

    sender = (
        env.get("sourceNumber")
        or env.get("sourceUuid")
        or env.get("source")
        or ""
    )
    if not sender:
        return None

    dm = env.get("dataMessage")
    if not isinstance(dm, dict):
        return None

    # Must have groupInfo to be a group message
    group_info = dm.get("groupInfo")
    if not isinstance(group_info, dict):
        return None
    group_id = group_info.get("groupId")
    if not group_id:
        return None

    text = dm.get("message") or dm.get("body") or ""

    reply_to_id = None
    quote = dm.get("quote")
    if isinstance(quote, dict):
        qid = quote.get("id") or quote.get("timestamp")
        if qid is not None:
            reply_to_id = str(qid)

    image_paths: list[str] = []
    attachments = dm.get("attachments") or []
    if isinstance(attachments, list):
        for a in attachments:
            if not isinstance(a, dict):
                continue
            ct = (a.get("contentType") or "").lower()
            if not ct.startswith("image/"):
                continue
            stored = a.get("storedFilename") or a.get("path") or a.get("file") or a.get("filename")
            if stored:
                image_paths.append(str(stored))

    message_id = str(dm.get("id") or dm.get("timestamp") or f"{group_id}:{sender}:{ts_i}")

    return InboundGroupMessage(
        message_id=message_id,
        group_id=str(group_id),
        sender=str(sender),
        ts=ts_i,
        text=str(text or ""),
        image_paths=image_paths,
        reply_to_id=reply_to_id,
    )


def _parse_direct_message(obj: dict) -> Optional[InboundDirectMessage]:
    """Parse a direct (1:1) message from signal-cli JSON."""
    env = obj.get("envelope") if isinstance(obj, dict) else None
    if not isinstance(env, dict):
        return None

    ts = env.get("timestamp")
    if ts is None:
        return None
    try:
        ts_i = int(ts)
    except Exception:
        return None

    sender = (
        env.get("sourceNumber")
        or env.get("sourceUuid")
        or env.get("source")
        or ""
    )
    if not sender:
        return None

    dm = env.get("dataMessage")
    if not isinstance(dm, dict):
        return None

    # Must NOT have groupInfo to be a direct message
    group_info = dm.get("groupInfo")
    if group_info is not None:
        return None

    text = dm.get("message") or dm.get("body") or ""

    image_paths: list[str] = []
    attachments = dm.get("attachments") or []
    if isinstance(attachments, list):
        for a in attachments:
            if not isinstance(a, dict):
                continue
            ct = (a.get("contentType") or "").lower()
            if not ct.startswith("image/"):
                continue
            stored = a.get("storedFilename") or a.get("path") or a.get("file") or a.get("filename")
            if stored:
                image_paths.append(str(stored))

    message_id = str(dm.get("id") or dm.get("timestamp") or f"dm:{sender}:{ts_i}")

    return InboundDirectMessage(
        message_id=message_id,
        sender=str(sender),
        ts=ts_i,
        text=str(text or ""),
        image_paths=image_paths,
    )
