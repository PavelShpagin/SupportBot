"""
Signal History Ingestion Service

Supports two modes:
1. signal-cli (legacy): Links as device, receives only NEW messages after linking.
   The 45-day history sync does NOT work with signal-cli.

2. Signal Desktop (recommended): Uses Signal Desktop running headlessly with Xvfb.
   This supports real history transfer - the 45-day sync actually works.
   Set USE_SIGNAL_DESKTOP=true to enable this mode.

Flow:
1. Link as a device (user scans QR)
2. Receive messages (via signal-cli or Signal Desktop DB)
3. Extract solved support cases using LLM
4. Post cases to signal-bot for RAG indexing
"""
from __future__ import annotations

import json
import logging
import re
import select
import shutil
import subprocess
import time
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Any, Dict, List, Optional

import httpx
import qrcode
import cv2
import numpy as np
from openai import OpenAI

from ingest.config import load_settings
from ingest.db import claim_next_job, complete_job, create_db, fail_job, is_job_cancelled

HISTORY_LINK = "HISTORY_LINK"
HISTORY_SYNC = "HISTORY_SYNC"

P_BLOCKS_SYSTEM = """З довгого фрагменту історії чату витягни вирішені кейси підтримки.
Поверни ТІЛЬКИ JSON з ключем:
- cases: масив об'єктів, кожен з:
  - case_block: рядок (підмножина сирих повідомлень)
НЕ повертай відкриті/невирішені кейси.

Правила:
- Кожен case_block повинен містити і проблему, і рішення.
- Ігноруй привітання та нерелевантну балаканину.
- Зберігай case_block як точні витяги з фрагменту.
"""

log = logging.getLogger(__name__)


class RetrySoon(Exception):
    pass


class JobCancelled(Exception):
    """Raised when job was cancelled by user."""
    pass


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _signal_cli_available(bin_name: str) -> bool:
    return shutil.which(bin_name) is not None


def _extract_linking_uri(output: str) -> str:
    """Extract sgnl:// or tsdevice: URI from signal-cli link output."""
    # Try sgnl:// format first (newer signal-cli versions)
    m = re.search(r"(sgnl://linkdevice\?[^\s]+)", output)
    if m:
        return m.group(1)
    # Fall back to tsdevice: format (older versions)
    m = re.search(r"(tsdevice:[^\s]+)", output)
    if m:
        return m.group(1)
    raise RuntimeError("Could not find linking URI in signal-cli output")


# ─────────────────────────────────────────────────────────────────────────────
# Signal-CLI Operations
# ─────────────────────────────────────────────────────────────────────────────

def _parse_receive_json(obj: dict) -> Optional[dict]:
    """Parse a single JSON message from signal-cli receive output.

    signal-cli can emit messages in multiple envelope shapes:
    - envelope.dataMessage (normal inbound messages)
    - envelope.syncMessage.sentMessage (sync transcripts, incl. during device linking)

    For history bootstrap we accept both, and we support both `groupInfo` and
    `groupV2`-style group identifiers.
    """
    env = obj.get("envelope") if isinstance(obj, dict) else None
    if not isinstance(env, dict):
        return None

    def _extract_group_id(msg: dict) -> Optional[str]:
        # Common shape: groupInfo.groupId
        gi = msg.get("groupInfo")
        if isinstance(gi, dict):
            gid = gi.get("groupId") or gi.get("id")
            if gid:
                return str(gid)

        # Newer shape: groupV2 (ids differ by implementation)
        gv2 = msg.get("groupV2")
        if isinstance(gv2, dict):
            gid = gv2.get("id") or gv2.get("groupId") or gv2.get("masterKey")
            if gid:
                return str(gid)

        # Fallbacks (rare)
        gid = msg.get("groupId") or msg.get("group_id")
        if gid:
            return str(gid)
        return None

    def _extract_text(msg: dict) -> str:
        return str(msg.get("message") or msg.get("body") or msg.get("text") or "")

    def _extract_ts(envelope_ts: Any, msg: dict) -> Optional[int]:
        # Prefer envelope timestamp; fallback to message timestamp.
        raw = envelope_ts if envelope_ts is not None else (msg.get("timestamp") or msg.get("sentTimestamp"))
        if raw is None:
            return None
        try:
            return int(raw)
        except Exception:
            return None

    def _extract_sender(msg: dict) -> str:
        # For normal inbound messages, these exist on the envelope.
        sender = (
            env.get("sourceUuid")
            or env.get("sourceNumber")
            or env.get("source")
            or env.get("sourceAddress")
            or ""
        )
        if sender:
            return str(sender)
        # For sync transcripts we may not have the original author; keep it stable.
        author = msg.get("sender") or msg.get("author") or ""
        return str(author or "sync")

    envelope_ts = env.get("timestamp")

    # 1) Normal inbound message
    dm = env.get("dataMessage")
    if isinstance(dm, dict):
        group_id = _extract_group_id(dm)
        if group_id:
            ts_i = _extract_ts(envelope_ts, dm)
            if ts_i is None:
                return None
            return {
                "group_id": group_id,
                "sender": _extract_sender(dm),
                "ts": ts_i,
                "text": _extract_text(dm),
            }

    # 2) Sync transcript (e.g., sentMessage during linking / history transfer)
    sync = env.get("syncMessage")
    if isinstance(sync, dict):
        sent = sync.get("sentMessage") or sync.get("sent")
        if isinstance(sent, dict):
            group_id = _extract_group_id(sent)
            if group_id:
                ts_i = _extract_ts(envelope_ts, sent)
                if ts_i is None:
                    return None
                return {
                    "group_id": group_id,
                    "sender": _extract_sender(sent),
                    "ts": ts_i,
                    "text": _extract_text(sent),
                }

    return None


def _collect_messages(*, settings, target_group_id: str) -> List[dict]:
    """
    Run signal-cli receive and collect messages for the target group.
    
    Note: This only receives NEW messages from the moment of linking.
    Historical 45-day sync is NOT supported by signal-cli.
    """
    if not _signal_cli_available(settings.signal_cli):
        raise RuntimeError(f"signal-cli binary not found: {settings.signal_cli}")

    cmd = [
        settings.signal_cli,
        "--output", "json",
        "--config", settings.signal_ingest_storage,
        "receive",
        "--timeout", "-1",  # Block until messages arrive
    ]

    log.info("Starting signal-cli receive...")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    assert proc.stdout is not None
    assert proc.stderr is not None

    q_lines: Queue[str] = Queue()

    def _read_stdout() -> None:
        for line in proc.stdout:
            if line:
                q_lines.put(line)

    def _read_stderr() -> None:
        for line in proc.stderr:
            if line and line.strip():
                log.info("signal-cli stderr: %s", line.strip())

    Thread(target=_read_stdout, daemon=True).start()
    Thread(target=_read_stderr, daemon=True).start()

    messages: List[dict] = []
    buf = ""
    seen_target = False
    last_target_time = time.time()
    deadline = time.time() + settings.history_max_seconds

    while time.time() < deadline:
        try:
            line = q_lines.get(timeout=1.0)
        except Empty:
            # Only stop early if we've collected at least one *target group* message and
            # then had an idle period. Otherwise keep waiting until the deadline.
            if seen_target and (time.time() - last_target_time) > settings.history_idle_seconds:
                log.info("Idle timeout reached, stopping receive")
                break
            continue

        buf += line
        
        try:
            obj = json.loads(buf)
        except json.JSONDecodeError:
            continue

        buf = ""
        parsed = _parse_receive_json(obj)
        if parsed is None or parsed["group_id"] != target_group_id:
            continue
        messages.append(parsed)
        seen_target = True
        last_target_time = time.time()
        log.debug("Received message: ts=%s text=%s...", parsed["ts"], parsed["text"][:50] if parsed["text"] else "")

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

    return messages


def _collect_messages_from_desktop(*, settings, target_group_id: str, target_group_name: str | None = None) -> List[dict]:
    """
    Collect messages from Signal Desktop service.
    
    This actually gets historical messages (45-day sync works with Signal Desktop).
    """
    url = settings.signal_desktop_url.rstrip("/")
    
    # First check if Signal Desktop is available
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{url}/status")
            r.raise_for_status()
            status = r.json()
            if not status.get("linked"):
                raise RuntimeError("Signal Desktop not linked yet")
    except httpx.ConnectError:
        raise RuntimeError(f"Signal Desktop service not reachable at {url}")
    
    # Get messages for the group
    try:
        params = {"limit": 800}
        if target_group_name:
            params["group_name"] = target_group_name
        
        with httpx.Client(timeout=60) as client:
            r = client.get(f"{url}/group/{target_group_id}/messages", params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.exception("Failed to get messages from Signal Desktop")
        raise RuntimeError(f"Failed to get messages from Signal Desktop: {e}")
    
    messages: List[dict] = []
    for m in data.get("messages", []):
        if not m.get("body"):
            continue
        messages.append({
            "group_id": m.get("group_id") or target_group_id,
            "sender": m.get("sender") or "unknown",
            "ts": m.get("timestamp", 0),
            "text": m.get("body", ""),
        })
    
    log.info("Collected %d messages from Signal Desktop for group_id=%s", len(messages), target_group_id)
    return messages


def _wait_for_desktop_history_sync(*, settings, timeout_seconds: int = 300) -> bool:
    """
    Wait for Signal Desktop to sync history after linking.
    
    Signal Desktop receives the 45-day history in the background after linking.
    We poll the status endpoint until we see messages appearing.
    """
    url = settings.signal_desktop_url.rstrip("/")
    start_time = time.time()
    last_count = 0
    stable_count = 0
    stable_threshold = 3  # Need 3 consecutive polls with same count
    
    log.info("Waiting for Signal Desktop history sync (timeout=%ds)...", timeout_seconds)
    
    while time.time() - start_time < timeout_seconds:
        try:
            with httpx.Client(timeout=10) as client:
                r = client.get(f"{url}/status")
                r.raise_for_status()
                status = r.json()
                
                if not status.get("linked"):
                    log.info("Signal Desktop not linked yet...")
                    time.sleep(5)
                    continue
                
                # Check conversations count as proxy for sync progress
                conv_count = status.get("conversations_count", 0)
                log.info("Signal Desktop status: linked=%s, conversations=%d",
                        status.get("linked"), conv_count)
                
                if conv_count == last_count and conv_count > 0:
                    stable_count += 1
                    if stable_count >= stable_threshold:
                        log.info("Signal Desktop sync appears complete (stable at %d conversations)", conv_count)
                        return True
                else:
                    stable_count = 0
                    last_count = conv_count
                
        except Exception as e:
            log.warning("Failed to check Signal Desktop status: %s", e)
        
        time.sleep(10)
    
    log.warning("Timed out waiting for Signal Desktop sync")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# LLM Case Extraction
# ─────────────────────────────────────────────────────────────────────────────

def _chunk_messages(*, messages: List[dict], max_chars: int, overlap_messages: int) -> List[str]:
    """Split messages into chunks for LLM processing."""
    formatted = [f'{m["sender"]} ts={m["ts"]}\n{m["text"]}\n' for m in messages if m.get("text")]
    chunks: List[str] = []
    cur: List[str] = []
    
    for line in formatted:
        candidate = "".join(cur) + line
        if len(candidate) > max_chars and cur:
            chunks.append("".join(cur))
            cur = cur[-overlap_messages:] if overlap_messages > 0 else []
        cur.append(line)
    
    if cur:
        chunks.append("".join(cur))
    
    return chunks


def _extract_case_blocks(*, openai_client: OpenAI, model: str, chunk_text: str) -> List[str]:
    """Extract solved support cases from a chunk of messages."""
    resp = openai_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": P_BLOCKS_SYSTEM},
            {"role": "user", "content": f"HISTORY_CHUNK:\n{chunk_text}"},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    raw = resp.choices[0].message.content or "{}"
    data = json.loads(raw)
    out: List[str] = []
    cases = data.get("cases", [])
    if isinstance(cases, list):
        for c in cases:
            if isinstance(c, dict) and isinstance(c.get("case_block"), str) and c["case_block"].strip():
                out.append(c["case_block"].strip())
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Signal-Bot Communication
# ─────────────────────────────────────────────────────────────────────────────

def _notify_qr_ready(*, settings, token: str, admin_id: str, group_name: str, qr_path: str) -> None:
    """Notify signal-bot that QR is ready."""
    payload = {"token": token, "admin_id": admin_id, "group_name": group_name, "qr_path": str(qr_path)}
    url = settings.signal_bot_url.rstrip("/") + "/history/qr-ready"
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
            log.info("Notified signal-bot: QR ready")
    except Exception:
        log.exception("Failed to notify QR ready")


def _notify_scan_received(*, settings, token: str) -> None:
    """Notify signal-bot that QR was scanned."""
    payload = {"token": token}
    url = settings.signal_bot_url.rstrip("/") + "/history/scan-received"
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
    except Exception:
        log.exception("Failed to notify scan received")


def _notify_progress(*, settings, token: str, progress_key: str, **kwargs) -> None:
    """Send progress update to signal-bot."""
    payload = {"token": token, "progress_key": progress_key, **kwargs}
    url = settings.signal_bot_url.rstrip("/") + "/history/progress"
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
    except Exception:
        log.exception("Failed to notify progress")


def _notify_link_result(
    *,
    settings,
    token: str,
    success: bool,
    message_count: int | None = None,
    cases_found: int | None = None,
    cases_inserted: int | None = None,
    note: str | None = None,
) -> None:
    """Notify signal-bot of link success/failure + optional summary metrics."""
    payload: Dict[str, Any] = {"token": token, "success": success}
    if message_count is not None:
        payload["message_count"] = int(message_count)
    if cases_found is not None:
        payload["cases_found"] = int(cases_found)
    if cases_inserted is not None:
        payload["cases_inserted"] = int(cases_inserted)
    if note:
        payload["note"] = str(note)
    url = settings.signal_bot_url.rstrip("/") + "/history/link-result"
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
            log.info("Notified link result: success=%s", success)
    except Exception:
        log.exception("Failed to notify link result")


def _post_cases_to_bot(*, settings, token: str, group_id: str, case_blocks: List[str]) -> int:
    """Post extracted cases to signal-bot for RAG indexing. Returns cases_inserted."""
    payload = {"token": token, "group_id": group_id, "cases": [{"case_block": b} for b in case_blocks]}
    url = settings.signal_bot_url.rstrip("/") + "/history/cases"
    with httpx.Client(timeout=60) as client:
        r = client.post(url, json=payload)
        r.raise_for_status()
        data = {}
        try:
            data = r.json() if r.content else {}
        except Exception:
            data = {}
        inserted = int(data.get("cases_inserted", 0)) if isinstance(data, dict) else 0
        log.info("Posted %d cases to signal-bot (inserted=%d)", len(case_blocks), inserted)
        return inserted


# ─────────────────────────────────────────────────────────────────────────────
# Cleanup
# ─────────────────────────────────────────────────────────────────────────────

def _cleanup_linked_session(*, settings, token: str) -> None:
    """Clean up signal-cli data after sync."""
    config_path = Path(settings.signal_ingest_storage)
    
    if not config_path.exists():
        log.info("No signal-cli config to clean up")
        return
    
    try:
        for item in config_path.iterdir():
            if item.is_dir() and item.name not in (".", ".."):
                shutil.rmtree(item)
                log.info("Removed signal-cli session data: %s", item)
            elif item.is_file():
                item.unlink()
                log.info("Removed signal-cli session file: %s", item)
        
        log.info("Cleaned up linked session")
    except Exception:
        log.exception("Failed to clean up signal-cli session")


# ─────────────────────────────────────────────────────────────────────────────
# Signal Desktop QR Flow (for real 45-day history)
# ─────────────────────────────────────────────────────────────────────────────

def _reset_signal_desktop(*, settings) -> bool:
    """Reset Signal Desktop so it shows fresh linking QR."""
    url = settings.signal_desktop_url.rstrip("/")
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(f"{url}/reset")
            r.raise_for_status()
            log.info("Signal Desktop reset: %s", r.json())
            return True
    except Exception as e:
        log.exception("Failed to reset Signal Desktop")
        return False


def _get_desktop_screenshot(*, settings, crop: bool = True) -> bytes | None:
    """Get screenshot from Signal Desktop (contains QR code)."""
    url = settings.signal_desktop_url.rstrip("/")
    try:
        with httpx.Client(timeout=30) as client:
            r = client.get(f"{url}/screenshot", params={"crop_qr": str(crop).lower()})
            r.raise_for_status()
            return r.content
    except Exception as e:
        log.exception("Failed to get Signal Desktop screenshot")
        return None


def _crop_qr_code(image_bytes: bytes) -> bytes:
    """Detect and crop QR code from image bytes using OpenCV."""
    try:
        # Convert bytes to numpy array
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        
        if img is None:
            log.warning("Failed to decode image for QR detection")
            return image_bytes

        # Initialize QRCode detector
        detector = cv2.QRCodeDetector()
        retval, decoded_info, points, straight_qrcode = detector.detectAndDecodeMulti(img)
        
        if points is not None and len(points) > 0:
            # points is a list of arrays of points for each QR code
            # We assume there's only one relevant QR code (or take the first)
            pts = points[0]
            
            # Get bounding box
            x_min = int(min(p[0] for p in pts))
            y_min = int(min(p[1] for p in pts))
            x_max = int(max(p[0] for p in pts))
            y_max = int(max(p[1] for p in pts))
            
            # Add padding (e.g. 30px)
            pad = 30
            h, w, _ = img.shape
            x_min = max(0, x_min - pad)
            y_min = max(0, y_min - pad)
            x_max = min(w, x_max + pad)
            y_max = min(h, y_max + pad)
            
            # Crop
            cropped = img[y_min:y_max, x_min:x_max]
            
            # Encode back to PNG
            success, encoded_img = cv2.imencode('.png', cropped)
            if success:
                log.info("Successfully detected and cropped QR code")
                return encoded_img.tobytes()
        
        log.warning("No QR code detected in screenshot, returning full image")
        return image_bytes
        
    except Exception as e:
        log.exception("Failed to crop QR code: %s", e)
        return image_bytes


def _wait_for_desktop_linked(*, settings, timeout_seconds: int = 120) -> bool:
    """Wait for Signal Desktop to be linked (user scanned QR)."""
    url = settings.signal_desktop_url.rstrip("/")
    start_time = time.time()
    
    log.info("Waiting for Signal Desktop to be linked (timeout=%ds)...", timeout_seconds)
    
    while time.time() - start_time < timeout_seconds:
        try:
            with httpx.Client(timeout=10) as client:
                r = client.get(f"{url}/status")
                r.raise_for_status()
                status = r.json()
                
                if status.get("linked"):
                    log.info("Signal Desktop is now linked!")
                    return True
                    
                log.info("Signal Desktop not linked yet, waiting...")
        except Exception as e:
            log.warning("Failed to check Signal Desktop status: %s", e)
        
        time.sleep(3)
    
    log.warning("Timed out waiting for Signal Desktop to link")
    return False


def _handle_history_link_desktop(*, settings, db, job_id: int, payload: Dict[str, Any]) -> None:
    """
    Handle HISTORY_LINK job using Signal Desktop (for real 45-day history).
    
    Flow:
    1. Reset Signal Desktop (clear data, show fresh QR)
    2. Take screenshot of QR and send to admin
    3. Wait for admin to scan QR (Signal Desktop links to their account)
    4. Wait for Signal Desktop to sync history
    5. Read messages from Signal Desktop DB
    6. Extract cases with LLM
    7. Post cases to signal-bot
    
    This is the ONLY way to get real historical messages (45-day sync).
    """
    token = str(payload["token"])
    admin_id = str(payload["admin_id"])
    group_id = str(payload["group_id"])
    group_name = str(payload.get("group_name", ""))

    def check_cancelled():
        if is_job_cancelled(db, job_id=job_id):
            raise JobCancelled(f"Job {job_id} cancelled")

    out_dir = Path(settings.history_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    qr_path = out_dir / f"{token}.png"

    try:
        check_cancelled()

        # ─────────────────────────────────────────────────────────────────
        # Step 1: Reset Signal Desktop
        # ─────────────────────────────────────────────────────────────────
        log.info("Resetting Signal Desktop for fresh linking...")
        if not _reset_signal_desktop(settings=settings):
            raise RuntimeError("Failed to reset Signal Desktop")
        
        # Wait for Signal Desktop to restart and show QR
        time.sleep(8)

        # ─────────────────────────────────────────────────────────────────
        # Step 2: Get screenshot of QR and send to admin
        # ─────────────────────────────────────────────────────────────────
        check_cancelled()
        
        log.info("Taking screenshot of Signal Desktop QR...")
        # Get FULL screenshot, then crop locally
        screenshot = _get_desktop_screenshot(settings=settings, crop=False)
        if not screenshot:
            raise RuntimeError("Failed to get Signal Desktop screenshot")
        
        # Crop QR code
        screenshot = _crop_qr_code(screenshot)
        
        # Save screenshot as QR image
        with open(qr_path, "wb") as f:
            f.write(screenshot)
        log.info("Saved QR screenshot: %s", qr_path)
        
        _notify_qr_ready(
            settings=settings,
            token=token,
            admin_id=admin_id,
            group_name=group_name,
            qr_path=str(qr_path),
        )
        log.info("QR sent to admin, waiting for scan...")

        # ─────────────────────────────────────────────────────────────────
        # Step 3: Wait for Signal Desktop to link (admin scans QR)
        # ─────────────────────────────────────────────────────────────────
        check_cancelled()
        
        if not _wait_for_desktop_linked(settings=settings, timeout_seconds=120):
            log.warning("Timed out waiting for QR scan")
            _notify_link_result(settings=settings, token=token, success=False, note="Timed out waiting for QR scan")
            return

        log.info("Device linked successfully!")
        _notify_scan_received(settings=settings, token=token)

        # ─────────────────────────────────────────────────────────────────
        # Step 4: Wait for history sync and collect messages
        # ─────────────────────────────────────────────────────────────────
        _notify_progress(settings=settings, token=token, progress_key="collecting")

        log.info("Waiting for Signal Desktop to sync history...")
        _wait_for_desktop_history_sync(settings=settings, timeout_seconds=int(settings.history_max_seconds))
        
        msgs = _collect_messages_from_desktop(
            settings=settings,
            target_group_id=group_id,
            target_group_name=group_name,
        )

        if not msgs:
            log.warning("No messages collected from history for group_id=%s", group_id)
            _notify_link_result(
                settings=settings,
                token=token,
                success=True,
                message_count=0,
                cases_found=0,
                cases_inserted=0,
                note="No messages found in history. The group may be empty or history transfer wasn't selected.",
            )
            return

        log.info("Collected %d messages", len(msgs))
        _notify_progress(settings=settings, token=token, progress_key="found_messages", count=len(msgs))

        # ─────────────────────────────────────────────────────────────────
        # Step 5: Process messages - extract cases using LLM
        # ─────────────────────────────────────────────────────────────────
        check_cancelled()
        _notify_progress(settings=settings, token=token, progress_key="processing")

        # Sort by timestamp and split into chunks for LLM
        msgs_sorted = sorted(msgs, key=lambda m: m.get("ts", 0))
        chunks = _chunk_messages(
            messages=msgs_sorted,
            max_chars=settings.chunk_max_chars,
            overlap_messages=settings.chunk_overlap_messages,
        )
        log.info("Split into %d chunks for processing", len(chunks))

        # Extract cases from chunks
        openai_client = OpenAI(
            api_key=settings.openai_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
        case_blocks: List[str] = []
        for i, chunk in enumerate(chunks, 1):
            check_cancelled()
            log.info("Processing chunk %d/%d...", i, len(chunks))
            blocks = _extract_case_blocks(
                openai_client=openai_client,
                model=settings.model_blocks,
                chunk_text=chunk,
            )
            case_blocks.extend(blocks)
        log.info("Extracted %d case blocks", len(case_blocks))

        # ─────────────────────────────────────────────────────────────────
        # Step 6: Send cases to signal-bot
        # ─────────────────────────────────────────────────────────────────
        if case_blocks:
            check_cancelled()
            _post_cases_to_bot(
                settings=settings,
                token=token,
                group_id=group_id,
                case_blocks=case_blocks,
            )

        _notify_link_result(
            settings=settings,
            token=token,
            success=True,
            message_count=len(msgs),
            cases_found=len(case_blocks),
            cases_inserted=len(case_blocks),
        )

    except JobCancelled:
        raise
    except Exception as e:
        log.exception("History link failed")
        _notify_link_result(settings=settings, token=token, success=False, note=str(e))
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Main Job Handler (signal-cli based - legacy, no real history)
# ─────────────────────────────────────────────────────────────────────────────

def _handle_history_link(*, settings, db, job_id: int, payload: Dict[str, Any]) -> None:
    """
    Handle HISTORY_LINK job using signal-cli.
    
    Flow:
    1. Start signal-cli link command which outputs a QR URI
    2. Generate QR image and notify signal-bot to send it to admin
    3. Wait for admin to scan QR (signal-cli link completes)
    4. Run signal-cli receive to collect messages (new messages only!)
    5. Extract cases with LLM
    6. Post cases to signal-bot
    7. Cleanup
    
    NOTE: signal-cli does NOT receive historical messages. Only new messages
    from the moment of linking are received.
    """
    token = str(payload["token"])
    admin_id = str(payload["admin_id"])
    group_id = str(payload["group_id"])
    group_name = str(payload.get("group_name", ""))

    def check_cancelled():
        if is_job_cancelled(db, job_id=job_id):
            raise JobCancelled(f"Job {job_id} cancelled")

    if not _signal_cli_available(settings.signal_cli):
        raise RuntimeError(f"signal-cli binary not found: {settings.signal_cli}")

    out_dir = Path(settings.history_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    qr_path = out_dir / f"{token}.png"

    device_name = f"SupportBotIngest-{token[:8]}"
    link_proc: subprocess.Popen | None = None

    def _terminate_proc(p: subprocess.Popen | None, name: str) -> None:
        if p is None:
            return
        try:
            if p.poll() is None:
                log.info("Terminating %s (pid=%s)", name, p.pid)
                p.terminate()
                try:
                    p.wait(timeout=10)
                except Exception:
                    p.kill()
        except Exception:
            log.exception("Failed to terminate %s", name)

    try:
        check_cancelled()

        # ─────────────────────────────────────────────────────────────────
        # Step 1: Start signal-cli link and capture the QR URI
        # ─────────────────────────────────────────────────────────────────
        cmd = [
            settings.signal_cli,
            "--config", settings.signal_ingest_storage,
            "link",
            "-n", device_name,
        ]
        log.info("Starting signal-cli link: %s", " ".join(cmd))

        link_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )

        # Read output to find the linking URI
        uri = None
        uri_deadline = time.time() + 30  # 30s to get URI

        while time.time() < uri_deadline and uri is None:
            check_cancelled()

            if link_proc.poll() is not None:
                _, stderr = link_proc.communicate()
                raise RuntimeError(f"signal-cli link exited early (rc={link_proc.returncode}): {stderr}")

            ready, _, _ = select.select([link_proc.stdout, link_proc.stderr], [], [], 1.0)
            for stream in ready:
                line = stream.readline()
                if line:
                    line = line.strip()
                    log.info("signal-cli link: %s", line)
                    if "sgnl://" in line or "tsdevice:" in line:
                        try:
                            uri = _extract_linking_uri(line)
                            log.info("Got linking URI: %s...", uri[:60])
                        except Exception:
                            pass

        if uri is None:
            raise RuntimeError("Timed out waiting for signal-cli link URI")

        # ─────────────────────────────────────────────────────────────────
        # Step 2: Generate QR and notify signal-bot to send it to admin
        # ─────────────────────────────────────────────────────────────────
        img = qrcode.make(uri)
        img.save(qr_path)
        log.info("Generated QR PNG: %s", qr_path)

        _notify_qr_ready(
            settings=settings,
            token=token,
            admin_id=admin_id,
            group_name=group_name,
            qr_path=str(qr_path),
        )
        log.info("QR sent to admin, waiting for scan...")

        # ─────────────────────────────────────────────────────────────────
        # Step 3: Wait for signal-cli link to complete (admin scans QR)
        # ─────────────────────────────────────────────────────────────────
        link_timeout = 120  # 2 minutes to scan
        link_deadline = time.time() + link_timeout

        while time.time() < link_deadline:
            check_cancelled()

            if link_proc.poll() is not None:
                break

            ready, _, _ = select.select([link_proc.stdout, link_proc.stderr], [], [], 1.0)
            for stream in ready:
                line = stream.readline()
                if line:
                    log.info("signal-cli link: %s", line.strip())

            time.sleep(0.5)

        if link_proc.poll() is None:
            _terminate_proc(link_proc, "signal-cli link")
            link_proc = None
            log.warning("Timed out waiting for QR scan")
            _notify_link_result(settings=settings, token=token, success=False, note="Timed out waiting for QR scan")
            return

        exit_code = link_proc.returncode
        link_proc = None

        if exit_code != 0:
            log.warning("signal-cli link failed with exit code %d", exit_code)
            _notify_link_result(settings=settings, token=token, success=False, note=f"signal-cli link failed (exit_code={exit_code})")
            return

        log.info("Device linked successfully!")
        _notify_scan_received(settings=settings, token=token)

        # ─────────────────────────────────────────────────────────────────
        # Step 4: Collect messages
        # - If USE_SIGNAL_DESKTOP=true: get from Signal Desktop (real history!)
        # - Otherwise: use signal-cli receive (new messages only)
        # ─────────────────────────────────────────────────────────────────
        _notify_progress(settings=settings, token=token, progress_key="collecting")

        if settings.use_signal_desktop:
            log.info("Using Signal Desktop for history collection (45-day sync works!)")
            # Wait for Signal Desktop to finish syncing history
            _wait_for_desktop_history_sync(settings=settings, timeout_seconds=int(settings.history_max_seconds))
            msgs = _collect_messages_from_desktop(
                settings=settings,
                target_group_id=group_id,
                target_group_name=group_name,
            )
        else:
            log.info("Using signal-cli for message collection (new messages only)")
            msgs = _collect_messages(settings=settings, target_group_id=group_id)

        if not msgs:
            log.warning("No messages collected from history for group_id=%s", group_id)
            _notify_link_result(
                settings=settings,
                token=token,
                success=True,
                message_count=0,
                cases_found=0,
                cases_inserted=0,
                note=(
                    "No messages were received during bootstrap. "
                    "If you expected history, ensure you selected 'Transfer message history' when linking. "
                    "Otherwise, the bot will learn from new messages only."
                ),
            )
            _cleanup_linked_session(settings=settings, token=token)
            return

        log.info("Collected %d messages", len(msgs))
        _notify_progress(settings=settings, token=token, progress_key="found_messages", count=len(msgs))

        # ─────────────────────────────────────────────────────────────────
        # Step 5: Process messages - extract cases using LLM
        # ─────────────────────────────────────────────────────────────────
        chunks = _chunk_messages(
            messages=msgs,
            max_chars=settings.chunk_max_chars,
            overlap_messages=settings.chunk_overlap_messages,
        )
        log.info("Split into %d chunks for processing", len(chunks))

        openai_client = OpenAI(
            api_key=settings.openai_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
        
        case_blocks: List[str] = []
        for i, ch in enumerate(chunks):
            check_cancelled()
            if len(chunks) > 1:
                _notify_progress(settings=settings, token=token, progress_key="processing_chunk", current=i+1, total=len(chunks))
            case_blocks.extend(_extract_case_blocks(openai_client=openai_client, model=settings.model_blocks, chunk_text=ch))

        deduped = list(dict.fromkeys([b for b in case_blocks if b.strip()]))
        cases_inserted = 0
        note = None
        if deduped:
            _notify_progress(settings=settings, token=token, progress_key="saving_cases", count=len(deduped))
            cases_inserted = _post_cases_to_bot(settings=settings, token=token, group_id=group_id, case_blocks=deduped)
            log.info("Posted %d case blocks to knowledge base (inserted=%d)", len(deduped), cases_inserted)
        else:
            log.info("No solved cases found in messages")
            note = "Messages were received but no solved cases were extracted."

        # Clean up QR
        qr_path.unlink(missing_ok=True)

        _notify_link_result(
            settings=settings,
            token=token,
            success=True,
            message_count=len(msgs),
            cases_found=len(deduped),
            cases_inserted=cases_inserted,
            note=note,
        )

    except JobCancelled:
        log.info("Job %d was cancelled", job_id)
        raise
    finally:
        _terminate_proc(link_proc, "signal-cli link")

    # Cleanup linked session
    _cleanup_linked_session(settings=settings, token=token)


# ─────────────────────────────────────────────────────────────────────────────
# Main Loop
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    _configure_logging()
    settings = load_settings()
    db = create_db(settings)

    log.info("signal-ingest started (poll=%.2fs)", settings.worker_poll_seconds)
    if settings.use_signal_desktop:
        log.info("Mode: Signal Desktop (45-day history sync enabled)")
        log.info("Signal Desktop URL: %s", settings.signal_desktop_url)
    else:
        log.info("Mode: signal-cli (new messages only - no history sync)")

    while True:
        job = claim_next_job(db, allowed_types=[HISTORY_LINK, HISTORY_SYNC])
        if job is None:
            time.sleep(settings.worker_poll_seconds)
            continue

        try:
            if job.type == HISTORY_LINK:
                if settings.use_signal_desktop:
                    # Use Signal Desktop for real 45-day history sync
                    _handle_history_link_desktop(settings=settings, db=db, job_id=job.job_id, payload=job.payload)
                else:
                    # Fallback to signal-cli (new messages only)
                    _handle_history_link(settings=settings, db=db, job_id=job.job_id, payload=job.payload)
            else:
                raise RuntimeError(f"Unknown job type: {job.type}")

            complete_job(db, job_id=job.job_id)

        except JobCancelled:
            log.info("Job cancelled: id=%s", job.job_id)
        except Exception:
            log.exception("Job failed: id=%s type=%s", job.job_id, job.type)
            _notify_link_result(
                settings=settings,
                token=job.payload.get("token", ""),
                success=False,
            )
            fail_job(db, job_id=job.job_id, attempts=job.attempts)


if __name__ == "__main__":
    main()
