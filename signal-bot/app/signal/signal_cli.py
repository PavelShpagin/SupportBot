from __future__ import annotations

import json
import logging
import threading
import time
import shutil
import subprocess
from dataclasses import dataclass, field
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

QR_MESSAGE_UK = """Відскануйте цей QR-код у Signal протягом 10 хвилин (Налаштування -> Пов'язані пристрої -> Додати пристрій).

Після сканування я зможу обробити історію групи "{group_name}" та почати відповідати на питання.

Під час підключення на телефоні виберіть «Перенести історію повідомлень» (якщо зʼявиться).

Якщо після сканування пристрій не додався - QR-код застарів. Напишіть назву групи ще раз для нового коду."""

QR_MESSAGE_EN = """Scan this QR code in Signal within 10 minutes (Settings -> Linked Devices -> Link New Device).

After scanning, I'll be able to process the history of group "{group_name}" and start answering questions.

During linking on your phone, choose “Transfer Message History” (if prompted).

If no device was added after scanning - the QR code has expired. Send the group name again for a new code."""

SUCCESS_MESSAGE_UK = """Успішно підключено до групи "{group_name}"!

Бот тепер відстежує нові повідомлення в групі та навчатиметься з них.

Хочете підключити ще одну групу? Напишіть її назву."""

SUCCESS_MESSAGE_EN = """Successfully connected to group "{group_name}"!

The bot now monitors new messages in the group and will learn from them.

Want to connect another group? Send its name."""

SCAN_RECEIVED_UK = """QR-код відскановано! Підключаюсь до групи "{group_name}"..."""

SCAN_RECEIVED_EN = """QR code scanned! Connecting to group "{group_name}"..."""

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

SEARCHING_GROUP_UK = """Шукаю групу "{group_name}"..."""

SEARCHING_GROUP_EN = """Searching for group "{group_name}"..."""

PROCESSING_MESSAGE_UK = """Знайшов групу "{group_name}"! Генерую QR-код..."""

PROCESSING_MESSAGE_EN = """Found group "{group_name}"! Generating QR code..."""

LANG_CHANGED_UK = """Мову змінено на українську."""

LANG_CHANGED_EN = """Language changed to English."""

LANG_HELP_UK = """Команди для зміни мови:
/uk - українська
/en - English"""

LANG_HELP_EN = """Language commands:
/uk - Ukrainian
/en - English"""


# Helper to get message by language
def _msg(uk: str, en: str, lang: str = "uk") -> str:
    """Return message in specified language."""
    return uk if lang == "uk" else en


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


class InboundReaction(BaseModel):
    """Emoji reaction to a message in a group chat."""
    group_id: str
    sender: str
    target_ts: int  # timestamp of the message being reacted to
    target_author: str  # author of the message being reacted to
    emoji: str
    is_remove: bool = False  # True if reaction is being removed


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
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def _bin(self) -> str:
        return self.settings.signal_cli

    def _config(self) -> str:
        return self.settings.signal_bot_storage

    def _user(self) -> str:
        return self.settings.signal_bot_e164
    
    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        with self._lock:
            return subprocess.run(cmd, capture_output=True, text=True)

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
        mention_recipients: List[str] | None = None,
    ) -> None:
        """Send text message to a group with optional quote/reply and mentions."""
        self.assert_available()
        
        # Handle mentions manually by appending placeholders and calculating offsets
        # signal-cli format: start:length:recipient
        final_text = text
        mentions_arg = []
        
        if mention_recipients:
            placeholder = "[[MENTION_PLACEHOLDER]]"
            if placeholder in final_text:
                # Replace placeholder with mentions
                parts = final_text.split(placeholder, 1)
                prefix = parts[0]
                suffix = parts[1]
                
                current_text = prefix
                
                for recipient in mention_recipients:
                    # Add space if needed before mention
                    if current_text and not current_text.endswith(" ") and not current_text.endswith("\n"):
                        current_text += " "
                    
                    start = len(current_text.encode('utf-16-le')) // 2
                    current_text += "@"
                    mentions_arg.append(f"{start}:1:{recipient}")
                
                final_text = current_text + suffix
            else:
                # Append to end (legacy behavior)
                for recipient in mention_recipients:
                    # Simple strategy: Append " @"
                    start = len(final_text.encode('utf-16-le')) // 2 # Java uses UTF-16 code units
                    
                    # Check if text ends with newline, if not add space
                    if final_text and not final_text.endswith("\n") and not final_text.endswith(" "):
                        final_text += " "
                        start += 1
                    
                    final_text += "@"
                    mentions_arg.append(f"{start}:1:{recipient}")
                
        cmd = [
            self._bin(), "--config", self._config(), "-u", self._user(),
            "send", "-g", group_id, "-m", final_text,
        ]
        
        # Add mention arguments
        for m in mentions_arg:
            cmd.extend(["--mention", m])

        # Reply-to / quote support (signal-cli `send` flags).
        if quote_timestamp is not None:
            cmd.extend(["--quote-timestamp", str(int(quote_timestamp))])
        if quote_author:
            cmd.extend(["--quote-author", str(quote_author)])
        if quote_message:
            cmd.extend(["--quote-message", str(quote_message)])

        log.info("signal-cli send group_id=%s bytes=%s mentions=%s", group_id, len(final_text.encode("utf-8")), len(mentions_arg))
        proc = self._run(cmd)
        if proc.stdout:
            log.info("signal-cli stdout: %s", proc.stdout.strip())
        if proc.stderr:
            log.info("signal-cli stderr: %s", proc.stderr.strip())
        if proc.returncode != 0:
            raise RuntimeError(f"signal-cli send failed (exit {proc.returncode})")

    def send_direct_text(self, *, recipient: str, text: str) -> bool:
        """Send text message to a user (1:1 chat).
        
        Returns True if sent successfully, False if user appears to have blocked/removed us.
        """
        self.assert_available()
        cmd = [
            self._bin(), "--config", self._config(), "-u", self._user(),
            "send", "-m", text, recipient,
        ]
        log.info("signal-cli send direct recipient=%s bytes=%s", recipient, len(text.encode("utf-8")))
        proc = self._run(cmd)
        if proc.stdout:
            log.info("signal-cli stdout: %s", proc.stdout.strip())
        if proc.stderr:
            stderr_lower = proc.stderr.lower()
            log.info("signal-cli stderr: %s", proc.stderr.strip())
            # Detect blocked/removed user
            if "unregistered" in stderr_lower or "not found" in stderr_lower or "unknown" in stderr_lower:
                log.warning("User %s appears to have blocked/removed us", recipient)
                return False
        if proc.returncode != 0:
            raise RuntimeError(f"signal-cli send failed (exit {proc.returncode})")
        return True

    def send_direct_image(self, *, recipient: str, image_path: str, caption: str = "") -> None:
        """Send image to a user (1:1 chat) with optional caption."""
        self.assert_available()
        # Note: recipient must come before -a, otherwise -a consumes it as an attachment path
        cmd = [
            self._bin(), "--config", self._config(), "-u", self._user(),
            "send", recipient, "-a", image_path,
        ]
        if caption:
            cmd.extend(["-m", caption])
        log.info("signal-cli send image recipient=%s path=%s", recipient, image_path)
        proc = self._run(cmd)
        if proc.stdout:
            log.info("signal-cli stdout: %s", proc.stdout.strip())
        if proc.stderr:
            log.info("signal-cli stderr: %s", proc.stderr.strip())
        if proc.returncode != 0:
            raise RuntimeError(f"signal-cli send image failed (exit {proc.returncode})")

    # ─────────────────────────────────────────────────────────────────────────
    # Admin onboarding messages (with language support)
    # ─────────────────────────────────────────────────────────────────────────

    def send_onboarding_prompt(self, *, recipient: str, lang: str = "uk") -> bool:
        """Send the initial onboarding message asking for group name.
        
        Returns True if sent successfully, False if user blocked/removed us.
        """
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

    def send_scan_received_message(self, *, recipient: str, group_name: str, lang: str = "uk") -> None:
        """Send message when QR code is scanned and history processing starts."""
        text = _msg(
            SCAN_RECEIVED_UK.format(group_name=group_name),
            SCAN_RECEIVED_EN.format(group_name=group_name),
            lang
        )
        try:
            self.send_direct_text(recipient=recipient, text=text)
            log.info("Sent scan received message to %s for group %s (lang=%s)", recipient, group_name, lang)
        except Exception:
            log.exception("Failed to send scan received message to %s", recipient)

    def send_progress_message(self, *, recipient: str, group_name: str, progress_text: str, lang: str = "uk") -> None:
        """Send progress update during history processing."""
        text = _msg(
            PROGRESS_MESSAGE_UK.format(group_name=group_name, progress_text=progress_text),
            PROGRESS_MESSAGE_EN.format(group_name=group_name, progress_text=progress_text),
            lang
        )
        try:
            self.send_direct_text(recipient=recipient, text=text)
            log.info("Sent progress message to %s for group %s (lang=%s)", recipient, group_name, lang)
        except Exception:
            log.exception("Failed to send progress message to %s", recipient)

    def send_lang_changed(self, *, recipient: str, lang: str) -> None:
        """Send language change confirmation."""
        text = _msg(LANG_CHANGED_UK, LANG_CHANGED_EN, lang)
        try:
            self.send_direct_text(recipient=recipient, text=text)
            log.info("Sent lang changed message to %s (lang=%s)", recipient, lang)
        except Exception:
            log.exception("Failed to send lang changed message to %s", recipient)

    def list_contacts(self) -> Optional[set[str]]:
        """
        Return current Signal contacts known to this account.
        Returns None when the command fails.
        """
        self.assert_available()
        cmd = [
            self._bin(), "--output", "json", "--config", self._config(), "-u", self._user(),
            "listContacts",
        ]
        proc = self._run(cmd)
        if proc.returncode != 0:
            log.warning("signal-cli listContacts failed: %s", (proc.stderr or "").strip())
            return None

        raw = (proc.stdout or "").strip()
        if not raw:
            return set()

        contacts: set[str] = set()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Failed to parse listContacts output as JSON")
            return None

        items = data if isinstance(data, list) else data.get("contacts", []) if isinstance(data, dict) else []
        if not isinstance(items, list):
            return set()

        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("number", "phoneNumber", "uuid", "id"):
                val = item.get(key)
                if val:
                    contacts.add(str(val))
        return contacts

    # ─────────────────────────────────────────────────────────────────────────
    # List groups
    # ─────────────────────────────────────────────────────────────────────────

    def list_groups(self) -> list[GroupInfo]:
        """List all groups the bot is a member of."""
        self.assert_available()
        cmd = [
            self._bin(), "--output", "json", "--config", self._config(), "-u", self._user(),
            "listGroups", "-d",
        ]
        proc = self._run(cmd)
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
        on_reaction: Callable[[InboundReaction], None] | None = None,
        on_contact_removed: Callable[[str], None] | None = None,
    ) -> None:
        """
        Signal receive loop. Dispatches:
        - Group messages -> on_group_message
        - Direct (1:1) messages -> on_direct_message
        - Contact removed/blocked -> on_contact_removed(phone_number)
        """
        log.info("Starting Signal receive loop...")
        self.assert_available()

        timeout_seconds = 1  # Fast polling for instant response
        cmd = [
            self._bin(), "--output", "json", "--config", self._config(), "-u", self._user(),
            "receive",
            "--timeout", str(timeout_seconds),
        ]

        log.info("Signal receive loop cmd: %s", " ".join(cmd))
        while True:
            proc = self._run(cmd)
            if proc.stderr:
                for line in proc.stderr.splitlines():
                    if line.strip():
                        log.info("signal-cli stderr: %s", line.strip())

            if proc.returncode != 0:
                log.warning("signal-cli receive failed (rc=%s); restarting soon", proc.returncode)
                time.sleep(2)
                continue

            buf = ""
            for line in proc.stdout.splitlines(True):
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

                # Try parsing as reaction
                if on_reaction is not None:
                    reaction = _parse_reaction(obj)
                    if reaction is not None:
                        try:
                            on_reaction(reaction)
                        except Exception:
                            log.exception("on_reaction handler failed")
                        continue

                # Try parsing as contact removed/blocked event
                if on_contact_removed is not None:
                    removed_contact = _parse_contact_removed(obj)
                    if removed_contact is not None:
                        try:
                            on_contact_removed(removed_contact)
                        except Exception:
                            log.exception("on_contact_removed handler failed")
                        continue
            # Normal (timeout) exit: loop again.
            # Small pause to avoid starving other signal-cli commands that need the config lock.
            time.sleep(0.2)


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


def _parse_reaction(obj: dict) -> Optional[InboundReaction]:
    """Parse an emoji reaction from signal-cli JSON."""
    env = obj.get("envelope") if isinstance(obj, dict) else None
    if not isinstance(env, dict):
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

    # Must have groupInfo to be a group reaction
    group_info = dm.get("groupInfo")
    if not isinstance(group_info, dict):
        return None
    group_id = group_info.get("groupId")
    if not group_id:
        return None

    # Must have reaction field
    reaction = dm.get("reaction")
    if not isinstance(reaction, dict):
        return None

    emoji = reaction.get("emoji") or ""
    if not emoji:
        return None

    target_ts = reaction.get("targetSentTimestamp") or reaction.get("targetTimestamp")
    if target_ts is None:
        return None
    try:
        target_ts_i = int(target_ts)
    except Exception:
        return None

    target_author = (
        reaction.get("targetAuthorNumber")
        or reaction.get("targetAuthorUuid")
        or reaction.get("targetAuthor")
        or ""
    )

    is_remove = bool(reaction.get("isRemove", False))

    return InboundReaction(
        group_id=str(group_id),
        sender=str(sender),
        target_ts=target_ts_i,
        target_author=str(target_author),
        emoji=str(emoji),
        is_remove=is_remove,
    )


def _parse_contact_removed(obj: dict) -> Optional[str]:
    """
    Parse contact removed/blocked events from signal-cli JSON.
    
    When a user deletes/blocks the bot, Signal may send:
    1. A syncMessage with blockedNumbers update
    2. A receiptMessage with error for unregistered/blocked contact
    3. A contactsUpdate indicating contact was removed
    
    Returns the phone number of the contact who removed/blocked us, or None.
    """
    env = obj.get("envelope") if isinstance(obj, dict) else None
    if not isinstance(env, dict):
        return None

    sender = (
        env.get("sourceNumber")
        or env.get("sourceUuid")
        or env.get("source")
        or ""
    )

    # Check for syncMessage with contacts update (contact removed themselves)
    sync_msg = env.get("syncMessage")
    if isinstance(sync_msg, dict):
        # Check for blocked numbers list update
        blocked = sync_msg.get("blockedNumbers") or sync_msg.get("blocked")
        if isinstance(blocked, dict):
            numbers = blocked.get("numbers") or blocked.get("groupIds") or []
            if numbers:
                log.info("Received blocked numbers sync: %s", numbers)
                # This is our blocked list, not theirs - skip
        
        # Check for contacts sync that indicates removal
        contacts = sync_msg.get("contacts")
        if isinstance(contacts, dict):
            # Contact sync happened - might indicate changes
            log.debug("Received contacts sync message")

    # Check for receipt message with unregistered user error
    # This happens when we try to send to someone who blocked us
    receipt = env.get("receiptMessage")
    if isinstance(receipt, dict):
        # Delivery receipt with error
        error_msg = receipt.get("error") or ""
        if "unregistered" in str(error_msg).lower() or "blocked" in str(error_msg).lower():
            if sender:
                log.info("Contact appears to have blocked/removed us: %s (error: %s)", sender, error_msg)
                return str(sender)

    # Check for typing indicator stop that might indicate block
    # (less reliable but can be a signal)
    typing = env.get("typingMessage")
    if isinstance(typing, dict):
        # Just typing, not a removal
        pass

    # Check for callMessage errors (blocked contacts can't call)
    call_msg = env.get("callMessage")
    if isinstance(call_msg, dict):
        error = call_msg.get("error")
        if error and sender:
            log.info("Call error from %s: %s (may indicate block)", sender, error)

    # The most reliable detection: when we get a "stale devices" or 
    # "unregistered user" error on send, but that's handled elsewhere.
    # Here we look for explicit contact removal sync messages.
    
    # Check for dataMessage that's a contact card removal
    data_msg = env.get("dataMessage")
    if isinstance(data_msg, dict):
        # Check for end session message (user reset safety number / removed us)
        if data_msg.get("endSession"):
            if sender:
                log.info("Received end session from %s - they may have removed us", sender)
                return str(sender)

    return None
