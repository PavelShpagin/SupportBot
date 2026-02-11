from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Any, Dict, List, Optional

import httpx
import qrcode
from openai import OpenAI

from ingest.config import load_settings
from ingest.db import claim_next_job, complete_job, create_db, fail_job

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


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _signal_cli_available(bin_name: str) -> bool:
    return shutil.which(bin_name) is not None


def _extract_tsdevice_uri(output: str) -> str:
    """Extract tsdevice: or sgnl:// URI from signal-cli link output."""
    # Try sgnl:// format first (newer signal-cli versions)
    m = re.search(r"(sgnl://linkdevice\?[^\s]+)", output)
    if m:
        return m.group(1)
    # Fall back to tsdevice: format (older versions)
    m = re.search(r"(tsdevice:[^\s]+)", output)
    if m:
        return m.group(1)
    raise RuntimeError("Could not find linking URI in signal-cli output")


def _run_link_and_wait(*, settings, token: str, admin_id: str, group_name: str, notify_callback) -> bool:
    """
    Run signal-cli link command, immediately send QR when URI is available,
    then wait for scan completion. Returns True if device was linked successfully.
    """
    out_dir = Path(settings.history_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{token}.png"

    device_name = f"SupportBotIngest-{token[:8]}"
    if not _signal_cli_available(settings.signal_cli):
        raise RuntimeError(f"signal-cli binary not found: {settings.signal_cli}")

    cmd = [
        settings.signal_cli,
        "--config",
        settings.signal_ingest_storage,
        "link",
        "-n",
        device_name,
    ]

    log.info("Starting link process (device_name=%s)", device_name)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    
    uri_found = False
    qr_sent = False
    linked = False
    
    # Read output in real-time to catch the URI immediately
    def read_and_process():
        nonlocal uri_found, qr_sent, linked
        output_lines = []
        
        # Read stdout and stderr together
        import select
        while proc.poll() is None:
            # Check if there's data to read
            ready, _, _ = select.select([proc.stdout, proc.stderr], [], [], 0.5)
            for stream in ready:
                line = stream.readline()
                if line:
                    line = line.strip()
                    output_lines.append(line)
                    log.info("signal-cli: %s", line)
                    
                    # Check for URI in output
                    if not uri_found and ("sgnl://" in line or "tsdevice:" in line):
                        try:
                            uri = _extract_tsdevice_uri(line)
                            uri_found = True
                            
                            # Immediately generate QR and notify
                            img = qrcode.make(uri)
                            img.save(out_path)
                            log.info("QR generated: %s", out_path)
                            
                            # Send notification to bot to deliver QR
                            notify_callback(str(out_path))
                            qr_sent = True
                            log.info("QR notification sent, waiting for scan...")
                        except Exception as e:
                            log.error("Failed to process URI: %s", e)
                    
                    # Check for successful link completion
                    if "Linked" in line or "linked successfully" in line.lower():
                        linked = True
                        log.info("Device linked successfully!")
        
        # Read any remaining output
        remaining_out, remaining_err = proc.communicate()
        if remaining_out:
            for line in remaining_out.strip().split('\n'):
                if line:
                    output_lines.append(line)
                    log.info("signal-cli stdout: %s", line)
        if remaining_err:
            for line in remaining_err.strip().split('\n'):
                if line:
                    output_lines.append(line)
                    log.info("signal-cli stderr: %s", line)
        
        return output_lines
    
    output_lines = read_and_process()
    
    # Check exit code
    exit_code = proc.returncode
    log.info("Link process exited with code %d", exit_code)
    
    # Exit code 0 means successful link
    if exit_code == 0:
        linked = True
    
    return linked, out_path if qr_sent else None


def _ensure_qr_png(*, settings, token: str) -> Path:
    """Legacy function for backward compatibility - just generates QR from a fresh link attempt."""
    out_dir = Path(settings.history_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{token}.png"
    if out_path.exists():
        return out_path

    device_name = f"SupportBotIngest-{token[:8]}"
    if not _signal_cli_available(settings.signal_cli):
        raise RuntimeError(f"signal-cli binary not found: {settings.signal_cli}")

    cmd = [
        settings.signal_cli,
        "--config",
        settings.signal_ingest_storage,
        "link",
        "-n",
        device_name,
    ]

    log.info("Generating link QR via signal-cli (device_name=%s)", device_name)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    
    uri = None
    # Read stdout line by line to get URI as soon as it's available
    for line in proc.stdout:
        line = line.strip()
        if line:
            log.info("signal-cli stdout: %s", line)
            if "sgnl://" in line or "tsdevice:" in line:
                try:
                    uri = _extract_tsdevice_uri(line)
                    break
                except RuntimeError:
                    pass
    
    if uri is None:
        # Also check stderr
        stderr_output = proc.stderr.read()
        if stderr_output:
            log.info("signal-cli stderr: %s", stderr_output.strip())
            if "sgnl://" in stderr_output or "tsdevice:" in stderr_output:
                try:
                    uri = _extract_tsdevice_uri(stderr_output)
                except RuntimeError:
                    pass
    
    if uri is None:
        proc.terminate()
        raise RuntimeError("Could not extract linking URI from signal-cli output")
    
    # Generate QR immediately
    img = qrcode.make(uri)
    img.save(out_path)
    log.info("Wrote QR PNG: %s (URI: %s...)", out_path, uri[:30])
    
    # Return path - process keeps running in background waiting for scan
    # Store the process so we can check it later
    return out_path, proc


def _parse_receive_json(obj: dict) -> Optional[dict]:
    env = obj.get("envelope") if isinstance(obj, dict) else None
    if not isinstance(env, dict):
        return None
    ts = env.get("timestamp")
    dm = env.get("dataMessage")
    if ts is None or not isinstance(dm, dict):
        return None
    group_info = dm.get("groupInfo")
    group_id = group_info.get("groupId") if isinstance(group_info, dict) else None
    if not group_id:
        return None
    sender = (
        env.get("sourceUuid")
        or env.get("sourceNumber")
        or env.get("source")
        or env.get("sourceAddress")
        or ""
    )
    text = dm.get("message") or dm.get("body") or ""
    try:
        ts_i = int(ts)
    except Exception:
        return None
    return {"group_id": str(group_id), "sender": str(sender), "ts": ts_i, "text": str(text or "")}


def _collect_history_messages(*, settings, admin_id: str, target_group_id: str) -> List[dict]:
    if not _signal_cli_available(settings.signal_cli):
        raise RuntimeError(f"signal-cli binary not found: {settings.signal_cli}")

    cmd = [
        settings.signal_cli,
        "--output",
        "json",
        "--config",
        settings.signal_ingest_storage,
        "-u",
        admin_id,
        "receive",
        "--timeout",
        "-1",
    ]

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
    seen_any = False
    last_line_time = time.time()
    deadline = time.time() + settings.history_max_seconds

    while time.time() < deadline:
        try:
            line = q_lines.get(timeout=1.0)
        except Empty:
            if seen_any and (time.time() - last_line_time) > settings.history_idle_seconds:
                break
            continue

        seen_any = True
        last_line_time = time.time()
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

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

    return messages


def _chunk_messages(*, messages: List[dict], max_chars: int, overlap_messages: int) -> List[str]:
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


def _post_cases_to_bot(*, settings, token: str, group_id: str, case_blocks: List[str]) -> None:
    payload = {"token": token, "group_id": group_id, "cases": [{"case_block": b} for b in case_blocks]}
    url = settings.signal_bot_url.rstrip("/") + "/history/cases"
    with httpx.Client(timeout=60) as client:
        r = client.post(url, json=payload)
        r.raise_for_status()
        log.info("Posted %s mined cases to signal-bot", len(case_blocks))


def _notify_qr_ready(*, settings, token: str, admin_id: str, group_name: str, qr_path: str) -> None:
    """Notify signal-bot that QR is ready so it can send to admin."""
    payload = {"token": token, "admin_id": admin_id, "group_name": group_name, "qr_path": str(qr_path)}
    url = settings.signal_bot_url.rstrip("/") + "/history/qr-ready"
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
            log.info("Notified signal-bot that QR is ready for admin %s", admin_id)
    except Exception:
        log.exception("Failed to notify signal-bot about QR ready")


def _notify_link_result(*, settings, token: str, success: bool) -> None:
    """Notify signal-bot of link success/failure."""
    payload = {"token": token, "success": success}
    url = settings.signal_bot_url.rstrip("/") + "/history/link-result"
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
            log.info("Notified signal-bot of link result: success=%s", success)
    except Exception:
        log.exception("Failed to notify signal-bot of link result")


def _notify_scan_received(*, settings, token: str) -> None:
    """Notify signal-bot that QR was scanned and processing started."""
    payload = {"token": token}
    url = settings.signal_bot_url.rstrip("/") + "/history/scan-received"
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
            log.info("Notified signal-bot: scan received")
    except Exception:
        log.exception("Failed to notify signal-bot of scan received")


def _notify_progress(*, settings, token: str, progress_key: str, **kwargs) -> None:
    """Send progress update to signal-bot.
    
    Args:
        progress_key: Key like 'collecting', 'found_messages', 'processing_chunk', 'saving_cases'
        kwargs: Variables for the message (e.g., count=5, total=10)
    """
    payload = {"token": token, "progress_key": progress_key, **kwargs}
    url = settings.signal_bot_url.rstrip("/") + "/history/progress"
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
            log.info("Notified signal-bot: progress update (%s)", progress_key)
    except Exception:
        log.exception("Failed to notify signal-bot of progress")


def _cleanup_linked_session(*, settings, token: str) -> None:
    """
    Clean up signal-cli data after history sync.
    
    This effectively "logs out" from the admin's account by removing
    the local signal-cli configuration. The linked device entry will
    remain in the admin's "Linked Devices" list as orphaned until
    they manually remove it, but:
    
    - Bot can no longer access admin's messages
    - Admin's account and data are completely unaffected
    - Admin can remove the orphaned entry anytime from their phone
    
    This is safe because signal-cli as a LINKED device cannot:
    - Delete the primary account
    - Remove other linked devices
    - Affect any data on the primary device
    """
    config_path = Path(settings.signal_ingest_storage)
    
    # Only clean up session-specific data, keep the base directory
    # Look for account-specific subdirectories
    if not config_path.exists():
        log.info("No signal-cli config to clean up")
        return
    
    try:
        # signal-cli stores account data in subdirectories named by phone number
        # or in data/ subdirectory. Clean everything for this session.
        for item in config_path.iterdir():
            if item.is_dir() and item.name not in (".", ".."):
                shutil.rmtree(item)
                log.info("Removed signal-cli session data: %s", item)
            elif item.is_file():
                item.unlink()
                log.info("Removed signal-cli session file: %s", item)
        
        log.info("Cleaned up linked session for token %s (admin should remove orphaned device from their phone)", token[:8])
    except Exception:
        log.exception("Failed to clean up signal-cli session (non-fatal)")


def _handle_history_link(*, settings, payload: Dict[str, Any]) -> None:
    token = str(payload["token"])
    admin_id = str(payload["admin_id"])
    group_id = str(payload["group_id"])
    group_name = str(payload.get("group_name", ""))

    out_dir = Path(settings.history_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    qr_path = out_dir / f"{token}.png"
    qr_sent_marker = out_dir / f"{token}.sent"  # Marker to track if QR was sent
    
    msgs = None  # Will be populated after successful link
    
    # Check if QR was already sent (from previous attempt)
    if qr_sent_marker.exists():
        log.info("QR was already sent for this token, checking if device is linked...")
        # Try to collect history - if device is linked, this should work
        msgs = _collect_history_messages(settings=settings, admin_id=admin_id, target_group_id=group_id)
        if msgs:
            log.info("Device is linked! Continuing with history processing...")
            # Clean up marker
            qr_sent_marker.unlink(missing_ok=True)
            qr_path.unlink(missing_ok=True)
            # Notify scan received and continue to processing
            _notify_scan_received(settings=settings, token=token)
        else:
            # Device not linked yet - just wait, don't regenerate QR
            log.info("Device not linked yet, waiting for scan...")
            raise RetrySoon("QR already sent, waiting for admin to scan")
    else:
        # First attempt - generate and send QR
        # Step 1: Start signal-cli link process and get URI immediately
        device_name = f"SupportBotIngest-{token[:8]}"
        if not _signal_cli_available(settings.signal_cli):
            raise RuntimeError(f"signal-cli binary not found: {settings.signal_cli}")

        cmd = [
            settings.signal_cli,
            "--config",
            settings.signal_ingest_storage,
            "link",
            "-n",
            device_name,
        ]

        log.info("Starting link process (device_name=%s)", device_name)
        proc = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.STDOUT,  # Combine stdout/stderr
            text=True, 
            bufsize=1
        )
        
        uri = None
        qr_sent = False
        linked = False
        
        # Read output line by line as it comes
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                    
                log.info("signal-cli: %s", line)
                
                # Look for URI
                if not uri and ("sgnl://" in line or "tsdevice:" in line):
                    try:
                        uri = _extract_tsdevice_uri(line)
                        log.info("Got linking URI: %s...", uri[:40])
                        
                        # Immediately generate QR
                        img = qrcode.make(uri)
                        img.save(qr_path)
                        log.info("Generated QR: %s", qr_path)
                        
                        # Immediately notify bot to send QR to admin
                        _notify_qr_ready(
                            settings=settings, 
                            token=token, 
                            admin_id=admin_id, 
                            group_name=group_name, 
                            qr_path=str(qr_path)
                        )
                        # Mark that QR was sent (prevents re-sending on retry)
                        qr_sent_marker.touch()
                        qr_sent = True
                        log.info("QR sent to admin, waiting for scan (up to 60s)...")
                    except Exception as e:
                        log.error("Failed to process URI: %s", e)
                
                # Check for successful link indicators
                if "Associated with" in line or "Successfully" in line.lower():
                    linked = True
                    log.info("Device linked successfully (from output)!")
                    # Don't break - let process exit naturally
                    
                # Log connection status but don't break - let process exit naturally
                if "Connection closed" in line:
                    log.info("Connection closed message received")
                    # Don't break! Wait for process to exit naturally
                    
        except Exception as e:
            log.exception("Error reading signal-cli output")
        
        # Wait for process to exit naturally (don't terminate prematurely!)
        try:
            proc.wait(timeout=10)  # Give it time to finish
        except subprocess.TimeoutExpired:
            log.warning("Link process didn't exit, terminating...")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        
        exit_code = proc.returncode
        log.info("Link process exited with code %d", exit_code)
        
        # Exit code 0 means successful link (most reliable indicator)
        if exit_code == 0:
            linked = True
            log.info("Device linked successfully (exit code 0)!")
        
        if not qr_sent:
            raise RuntimeError("Failed to generate or send QR code")
        
        if not linked:
            # QR was sent but not scanned in time - retry later
            raise RetrySoon("QR sent but not scanned yet - waiting for admin to scan")
        
        # Link succeeded on first attempt - clean up and notify
        qr_sent_marker.unlink(missing_ok=True)
        qr_path.unlink(missing_ok=True)
        _notify_scan_received(settings=settings, token=token)

    # At this point, either:
    # 1. We came from retry path with msgs already populated
    # 2. We came from first attempt path with link succeeded
    
    # Collect history messages if not already done
    if msgs is None:
        _notify_progress(settings=settings, token=token, progress_key="collecting")
        msgs = _collect_history_messages(settings=settings, admin_id=admin_id, target_group_id=group_id)
        if not msgs:
            raise RetrySoon("No history messages received yet (device may not be linked yet)")
    
    log.info("Collected %d messages from history", len(msgs))
    _notify_progress(settings=settings, token=token, progress_key="found_messages", count=len(msgs))

    # Step 4: Process history
    chunks = _chunk_messages(
        messages=msgs,
        max_chars=settings.chunk_max_chars,
        overlap_messages=settings.chunk_overlap_messages,
    )
    log.info("Split into %d chunks for processing", len(chunks))

    # Use Google's OpenAI-compatible endpoint for Gemini models
    openai_client = OpenAI(
        api_key=settings.openai_api_key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    case_blocks: List[str] = []
    for i, ch in enumerate(chunks):
        if len(chunks) > 1:
            _notify_progress(settings=settings, token=token, progress_key="processing_chunk", current=i+1, total=len(chunks))
        case_blocks.extend(_extract_case_blocks(openai_client=openai_client, model=settings.model_blocks, chunk_text=ch))

    deduped = list(dict.fromkeys([b for b in case_blocks if b.strip()]))
    if deduped:
        _notify_progress(settings=settings, token=token, progress_key="saving_cases", count=len(deduped))
        _post_cases_to_bot(settings=settings, token=token, group_id=group_id, case_blocks=deduped)
        log.info("Posted %d cases to knowledge base", len(deduped))
    else:
        log.info("No solved cases found in synced history (group_id=%s)", group_id)
    
    # Step 5: Notify success - processing complete!
    _notify_link_result(settings=settings, token=token, success=True)
    
    # Step 6: Clean up - "log out" from admin's account
    # This removes local signal-cli data so bot no longer has access to admin's messages.
    # Admin's account is completely unaffected - they just see an orphaned device entry
    # in Settings → Linked Devices which they can remove.
    _cleanup_linked_session(settings=settings, token=token)


def main() -> None:
    _configure_logging()
    settings = load_settings()
    db = create_db(settings)

    log.info("signal-ingest started (poll=%.2fs)", settings.worker_poll_seconds)

    while True:
        job = claim_next_job(db, allowed_types=[HISTORY_LINK, HISTORY_SYNC])
        if job is None:
            time.sleep(settings.worker_poll_seconds)
            continue

        max_link_attempts = 10  # Give user ~10 minutes to scan QR (10 attempts * ~60s each)
        try:
            if job.type == HISTORY_LINK:
                _handle_history_link(settings=settings, payload=job.payload)
            elif job.type == HISTORY_SYNC:
                raise NotImplementedError("HISTORY_SYNC is not used (history runs as part of HISTORY_LINK)")
            else:
                raise RuntimeError(f"Unknown job type: {job.type}")

            complete_job(db, job_id=job.job_id)
        except RetrySoon as e:
            log.warning("History not ready yet; will retry soon (job_id=%s): %s", job.job_id, e)
            # Check if we've exceeded max attempts
            if job.attempts + 1 >= max_link_attempts:
                log.warning("Max attempts reached for job %s, notifying failure", job.job_id)
                # Send failure notification to user
                token = job.payload.get("token", "")
                if token:
                    _notify_link_result(settings=settings, token=token, success=False)
            fail_job(db, job_id=job.job_id, attempts=job.attempts, max_attempts=max_link_attempts)
        except Exception:
            log.exception("Job failed: id=%s type=%s", job.job_id, job.type)
            # Send failure notification for unexpected errors
            if job.type == HISTORY_LINK:
                token = job.payload.get("token", "")
                if token:
                    _notify_link_result(settings=settings, token=token, success=False)
            fail_job(db, job_id=job.job_id, attempts=job.attempts)


if __name__ == "__main__":
    main()

