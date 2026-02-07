from __future__ import annotations

import json
import logging
import threading
import time
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from pydantic import BaseModel, Field

from app.config import Settings

log = logging.getLogger(__name__)


class InboundSignalMessage(BaseModel):
    message_id: str
    group_id: str
    sender: str
    ts: int
    text: str = ""
    image_paths: list[str] = Field(default_factory=list)
    reply_to_id: str | None = None


@dataclass(frozen=True)
class SignalCliAdapter:
    settings: Settings

    def _bin(self) -> str:
        return self.settings.signal_cli

    def assert_available(self) -> None:
        if shutil.which(self._bin()) is None:
            raise RuntimeError(f"signal-cli binary not found: {self._bin()}")

    def send_group_text(self, *, group_id: str, text: str) -> None:
        self.assert_available()
        cmd = [
            self._bin(),
            "--config",
            self.settings.signal_bot_storage,
            "-u",
            self.settings.signal_bot_e164,
            "send",
            "-g",
            group_id,
            "-m",
            text,
        ]

        log.info("signal-cli send group_id=%s bytes=%s", group_id, len(text.encode("utf-8")))
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.stdout:
            log.info("signal-cli stdout: %s", proc.stdout.strip())
        if proc.stderr:
            log.info("signal-cli stderr: %s", proc.stderr.strip())
        if proc.returncode != 0:
            raise RuntimeError(f"signal-cli send failed (exit {proc.returncode})")

    def listen_forever(self, *, on_message: Callable[[InboundSignalMessage], None]) -> None:
        """
        Best-effort Signal receive loop via signal-cli.

        Notes:
        - signal-cli output schemas vary by version; this parser is defensive.
        - message_id is derived from (group_id, sender, timestamp) when a native id is not present.
        """
        self.assert_available()

        cmd = [
            self._bin(),
            "--config",
            self.settings.signal_bot_storage,
            "-u",
            self.settings.signal_bot_e164,
            "receive",
            "--output",
            "json",
        ]

        while True:
            log.info("Starting signal-cli receive loop")
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
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
                    # Might be multi-line JSON; keep buffering.
                    continue
                finally:
                    # If parse succeeded, we reset below.
                    pass

                buf = ""
                msg = _parse_inbound(obj)
                if msg is None:
                    continue
                try:
                    on_message(msg)
                except Exception:
                    log.exception("on_message handler failed")

            rc = proc.wait(timeout=5)
            log.warning("signal-cli receive loop exited (rc=%s); restarting soon", rc)
            time.sleep(2)


def _parse_inbound(obj: dict) -> Optional[InboundSignalMessage]:
    """
    Parse best-effort group message from signal-cli JSON output.

    Expected shapes vary; we look for an envelope.dataMessage.groupInfo.groupId.
    """
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
        env.get("sourceUuid")
        or env.get("sourceNumber")
        or env.get("source")
        or env.get("sourceAddress")
        or ""
    )
    if not sender:
        return None

    dm = env.get("dataMessage")
    if not isinstance(dm, dict):
        return None

    group_info = dm.get("groupInfo")
    group_id = None
    if isinstance(group_info, dict):
        group_id = group_info.get("groupId")
    if not group_id:
        # Not a group message (or schema changed).
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

    return InboundSignalMessage(
        message_id=message_id,
        group_id=str(group_id),
        sender=str(sender),
        ts=ts_i,
        text=str(text or ""),
        image_paths=image_paths,
        reply_to_id=reply_to_id,
    )

