"""
Signal History Ingestion Service

Uses Signal Desktop with QR-based user linking to:
1. Reset Signal Desktop and generate QR code
2. Wait for user to scan QR (links their account temporarily)
3. Read historical messages from their groups (45-day sync)
4. Extract solved support cases using LLM
5. Post cases to signal-bot for RAG indexing
6. Reset Signal Desktop for next user

Each history import requires the user to scan a QR code.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List

import httpx
from openai import OpenAI

from ingest.config import load_settings
from ingest.db import claim_next_job, complete_job, create_db, fail_job, is_job_cancelled

HISTORY_LINK = "HISTORY_LINK"
HISTORY_SYNC = "HISTORY_SYNC"

P_BLOCKS_SYSTEM = """You analyze a chunk of support chat history and extract FULLY RESOLVED support cases.

Each message in the chunk is formatted as:
  sender_hash ts=TIMESTAMP msg_id=MESSAGE_ID
  message text

Return ONLY valid JSON with key:
- cases: array of objects, each with:
  - case_block: string (the EXACT messages from the chunk that form this case, problem through resolution, preserving all header lines with msg_id)

Rules:
- Extract ONLY solved cases with a confirmed working solution.
- Do NOT extract open/unresolved issues, greetings, or off-topic messages.
- Each case_block must include both the problem and the confirmed solution.
- Preserve the original message headers (sender_hash ts=... msg_id=...) verbatim inside case_block ? they are needed for evidence linking.
- Do not paraphrase or summarize; copy the exact message lines.
- If there are no solved cases, return {"cases": []}.

Resolution signals (from strongest to weakest):
- reactions=N (N > 0) on a technical answer message -- STRONG signal, treat as confirmed resolved
- Text confirmation after a technical answer (any language):
  English: "thanks", "working", "works", "ok", "solved", "it worked", "fixed"
  Ukrainian: "\u0434\u044f\u043a\u0443\u044e", "\u043f\u0440\u0430\u0446\u044e\u0454", "\u0432\u0438\u0440\u0456\u0448\u0435\u043d\u043e", "\u043e\u043a", "\u0437\u0430\u0440\u0430\u0431\u043e\u0442\u0430\u043b\u043e"
  Russian: "\u0441\u043f\u0430\u0441\u0438\u0431\u043e", "\u0437\u0430\u0440\u0430\u0431\u043e\u0442\u0430\u043b\u043e", "\u043f\u043e\u043c\u043e\u0433\u043b\u043e"
- The conversation thread ends after a technical answer (no follow-up questions)

Be generous: if a technical answer has any positive reaction OR brief confirmation, treat as solved.
"""

log = logging.getLogger(__name__)


class JobCancelled(Exception):
    """Raised when job was cancelled by user."""
    pass


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


# ?????????????????????????????????????????????????????????????????????????????
# Signal Desktop Operations
# ?????????????????????????????????????????????????????????????????????????????

def _check_desktop_status(settings) -> dict:
    """Check if Signal Desktop is linked and available."""
    url = settings.signal_desktop_url.rstrip("/") + "/status"
    try:
        with httpx.Client(timeout=30) as client:
            r = client.get(url)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        log.exception("Failed to check Signal Desktop status")
        raise RuntimeError(f"Signal Desktop not available: {e}")


def _reset_desktop(settings) -> dict:
    """Reset Signal Desktop to show QR code for new user linking."""
    url = settings.signal_desktop_url.rstrip("/") + "/reset"
    try:
        with httpx.Client(timeout=60) as client:
            r = client.post(url)
            # Reset may return 500 on permission errors but still work
            return r.json() if r.status_code == 200 else {"status": "reset_attempted"}
    except Exception as e:
        log.warning("Reset request failed (may still work): %s", e)
        return {"status": "reset_attempted"}


def _get_desktop_screenshot(settings) -> bytes:
    """Get screenshot from Signal Desktop (for QR code)."""
    url = settings.signal_desktop_url.rstrip("/") + "/screenshot"
    try:
        with httpx.Client(timeout=30) as client:
            r = client.get(url)
            r.raise_for_status()
            return r.content
    except Exception as e:
        log.exception("Failed to get screenshot")
        raise RuntimeError(f"Failed to get screenshot: {e}")


def _get_desktop_messages(settings, group_id: str, group_name: str, limit: int = 800) -> List[dict]:
    """Get messages from Signal Desktop for a specific group."""
    url = settings.signal_desktop_url.rstrip("/") + "/group/messages"
    params = {"group_id": group_id, "limit": limit, "group_name": group_name}
    
    try:
        with httpx.Client(timeout=120) as client:
            r = client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            messages = data.get("messages", [])
            # Log reaction stats
            with_reactions = [m for m in messages if m.get("reactions", 0) > 0]
            if with_reactions:
                log.info("Messages with reactions: %d/%d", len(with_reactions), len(messages))
                for m in with_reactions[:3]:
                    log.info("  reactions=%d: %s", m.get("reactions"), m.get("body", "")[:50])
            else:
                log.info("No messages with reactions found (total: %d)", len(messages))
            return messages
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            log.warning("Group not found in Signal Desktop: %s (%s)", group_name, group_id)
            return []
        raise


# ?????????????????????????????????????????????????????????????????????????????
# LLM Case Extraction
# ?????????????????????????????????????????????????????????????????????????????

def _chunk_messages(*, messages: List[dict], max_chars: int, overlap_messages: int) -> List[str]:
    """Split messages into chunks for LLM processing."""
    formatted = []
    for m in messages:
        text = m.get("text") or m.get("body") or ""
        if not text:
            continue
        sender = m.get("sender") or m.get("source") or "unknown"
        ts = m.get("ts") or m.get("timestamp") or 0
        msg_id = m.get("id") or m.get("message_id") or str(ts)
        reactions = int(m.get("reactions") or 0)
        header = f'{sender} ts={ts} msg_id={msg_id}'
        if reactions > 0:
            header += f' reactions={reactions}'
        formatted.append(f'{header}\n{text}\n')
    
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


# ?????????????????????????????????????????????????????????????????????????????
# Signal-Bot Communication
# ?????????????????????????????????????????????????????????????????????????????

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


def _notify_link_result(*, settings, token: str, success: bool, message_count: int = 0, cases_found: int = 0, cases_inserted: int | None = None, note: str = "") -> None:
    """Notify signal-bot of link success/failure."""
    payload = {
        "token": token,
        "success": success,
        "message_count": message_count,
        "cases_found": cases_found,
        "note": note,
    }
    if cases_inserted is not None:
        payload["cases_inserted"] = cases_inserted
    url = settings.signal_bot_url.rstrip("/") + "/history/link-result"
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
            log.info("Notified link result: success=%s messages=%d cases=%d", success, message_count, cases_found)
    except Exception:
        log.exception("Failed to notify link result")


def _send_qr_to_user(*, settings, token: str, qr_image: bytes) -> bool:
    """Send QR code image to user via signal-bot."""
    import base64
    payload = {
        "token": token,
        "qr_image_base64": base64.b64encode(qr_image).decode("utf-8"),
    }
    url = settings.signal_bot_url.rstrip("/") + "/history/qr-code"
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
            log.info("Sent QR code to user")
            return True
    except Exception:
        log.exception("Failed to send QR code to user")
        return False


def _post_cases_to_bot(*, settings, token: str, group_id: str, case_blocks: List[str], messages: List[dict]) -> int:
    """Post extracted cases to signal-bot for RAG indexing. Returns cases_inserted from response."""
    # Format messages for the API
    formatted_messages = []
    for m in messages:
        text = m.get("text") or m.get("body") or ""
        if not text:
            continue
        sender = m.get("sender") or m.get("source") or "unknown"
        sender_name = m.get("sender_name") or None
        ts = m.get("ts") or m.get("timestamp") or 0
        msg_id = m.get("id") or m.get("message_id") or str(ts)
        # Hash the sender for privacy
        import hashlib
        sender_hash = hashlib.sha256(sender.encode()).hexdigest()[:16]
        formatted_messages.append({
            "message_id": msg_id,
            "sender_hash": sender_hash,
            "sender_name": sender_name,
            "ts": ts,
            "content_text": text,
        })
    
    payload = {
        "token": token,
        "group_id": group_id,
        "cases": [{"case_block": b} for b in case_blocks],
        "messages": formatted_messages,  # Include raw messages for evidence linking
    }
    url = settings.signal_bot_url.rstrip("/") + "/history/cases"
    with httpx.Client(timeout=120) as client:  # Longer timeout for larger payloads
        r = client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()
        inserted = int(data.get("cases_inserted", 0))
        log.info("Posted %d cases + %d messages to signal-bot (%d inserted)", len(case_blocks), len(formatted_messages), inserted)
        return inserted


# ?????????????????????????????????????????????????????????????????????????????
# Main Job Handler - Signal Desktop with QR-based linking
# ?????????????????????????????????????????????????????????????????????????????

def _handle_history_link_desktop(*, settings, db, job_id: int, payload: Dict[str, Any]) -> None:
    """
    Handle HISTORY_LINK job using QR-based Signal Desktop linking.
    
    Flow:
    1. Reset Signal Desktop to show QR code
    2. Send QR code to user
    3. Wait for user to scan (poll until linked with user conversations)
    4. Read messages from group
    5. Extract cases with LLM
    6. Post cases to signal-bot
    """
    token = str(payload["token"])
    group_id = str(payload["group_id"])
    group_name = str(payload.get("group_name", ""))

    def check_cancelled():
        if is_job_cancelled(db, job_id=job_id):
            raise JobCancelled(f"Job {job_id} cancelled")

    try:
        check_cancelled()

        # Always reset and show QR for every group link (user requirement: ask for QR every time)
        # ?????????????????????????????????????????????????????????????????
        # Step 1: Reset Signal Desktop and get QR code
        # ?????????????????????????????????????????????????????????????????
        log.info("Resetting Signal Desktop for new user link...")
        _reset_desktop(settings)

        # Wait for Signal Desktop to restart and show QR
        time.sleep(10)

        # Get QR code screenshot
        log.info("Getting QR code screenshot...")
        qr_image = _get_desktop_screenshot(settings)

        if len(qr_image) < 1000:
            log.error("QR screenshot too small (%d bytes), Signal Desktop may not be ready", len(qr_image))
            _notify_link_result(
                settings=settings,
                token=token,
                success=False,
                note="Failed to get QR code. Signal Desktop may not be ready.",
            )
            return

        # Send QR to user
        if not _send_qr_to_user(settings=settings, token=token, qr_image=qr_image):
            _notify_link_result(
                settings=settings,
                token=token,
                success=False,
                note="Failed to send QR code to user.",
            )
            return

        _notify_progress(settings=settings, token=token, progress_key="qr_sent")

        # Step 2: Wait for user to scan QR code
        log.info("Waiting for user to scan QR code...")
        max_wait_seconds = 300  # 5 minutes to scan
        poll_interval = 3
        waited = 0

        reminder_sent = False
        while waited < max_wait_seconds:
            check_cancelled()
            time.sleep(poll_interval)
            waited += poll_interval

            status = _check_desktop_status(settings)
            if status.get("has_user_conversations"):
                log.info("User linked! Found %d conversations", status.get("conversations_count", 0))
                break

            # Send a reminder at the 2-minute mark (halfway)
            if not reminder_sent and waited >= 120:
                reminder_sent = True
                _notify_progress(settings=settings, token=token, progress_key="qr_reminder")
        else:
            log.warning("Timeout waiting for user to scan QR code")
            _notify_link_result(
                settings=settings,
                token=token,
                success=False,
                note="Timeout waiting for QR scan. Please try again.",
            )
            return

        log.info("Signal Desktop is linked, waiting for sync then verifying group: %s", group_name or group_id)

        # Security: Verify the linked admin account is in the target group.
        # This check is independent from the bot-side check in /history/cases.
        # Defense-in-depth: BOTH the admin and the bot must be in the group.
        #
        # After QR scan Signal Desktop needs time to sync conversations from the
        # server (typically 10-30s). We poll until the group appears or timeout.
        convs_url = settings.signal_desktop_url.rstrip("/") + "/conversations"
        group_name_lower = group_name.lower().strip() if group_name else ""
        sync_timeout = 60  # seconds to wait for group to appear after linking
        sync_poll = 5
        sync_waited = 0
        admin_in_group = False

        def _check_group_in_convs(convs_data: list) -> bool:
            for conv in convs_data:
                if conv.get("type") != "group":
                    continue
                if conv.get("groupId") == group_id or conv.get("id") == group_id:
                    return True
                if group_name_lower and (conv.get("name") or "").lower().strip() == group_name_lower:
                    return True
            return False

        try:
            while sync_waited <= sync_timeout:
                check_cancelled()
                with httpx.Client(timeout=30) as client:
                    resp = client.get(convs_url)
                    resp.raise_for_status()
                    convs_data = resp.json().get("conversations", [])

                if _check_group_in_convs(convs_data):
                    admin_in_group = True
                    log.info(
                        "Admin verified in group '%s' after %ds sync (%d conversations)",
                        group_name, sync_waited, len(convs_data)
                    )
                    break

                if sync_waited == 0:
                    log.info(
                        "Group '%s' not yet in admin's %d conversations ? waiting for sync...",
                        group_name, len(convs_data)
                    )
                    _notify_progress(settings=settings, token=token, progress_key="syncing")
                sync_waited += sync_poll
                if sync_waited <= sync_timeout:
                    time.sleep(sync_poll)

            if not admin_in_group:
                log.warning(
                    "SECURITY BLOCK: Admin is NOT in group '%s' (id=%s...) after %ds sync. "
                    "Admin has %d conversations.",
                    group_name, group_id[:20], sync_timeout, len(convs_data)
                )
                _notify_link_result(
                    settings=settings,
                    token=token,
                    success=False,
                    note=(
                        f"Your Signal account is not in group '{group_name}'. "
                        "Both you and the bot must be members to import history."
                    ),
                )
                return
        except JobCancelled:
            raise
        except Exception as e:
            log.error("Could not verify admin group membership ? blocking: %s", e)
            _notify_link_result(
                settings=settings,
                token=token,
                success=False,
                note="Could not verify group membership. Please try again.",
            )
            return

        # ?????????????????????????????????????????????????????????????????
        # Step 3: Fetch messages from Signal Desktop
        # ?????????????????????????????????????????????????????????????????
        _notify_progress(settings=settings, token=token, progress_key="collecting")
        
        msgs = _get_desktop_messages(settings, group_id=group_id, group_name=group_name)
        
        if not msgs:
            log.warning("No messages found for group: %s", group_name or group_id)
            _notify_link_result(
                settings=settings,
                token=token,
                success=True,
                message_count=0,
                cases_found=0,
                note="No messages found in group. The group may not exist or has no messages.",
            )
            return

        log.info("Fetched %d messages from Signal Desktop", len(msgs))
        _notify_progress(settings=settings, token=token, progress_key="found_messages", count=len(msgs))

        check_cancelled()

        # ?????????????????????????????????????????????????????????????????
        # Step 3: Process messages - extract cases using LLM
        # ?????????????????????????????????????????????????????????????????
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
        
        # ?????????????????????????????????????????????????????????????????
        # Step 4: Post cases to signal-bot
        # ?????????????????????????????????????????????????????????????????
        cases_inserted = 0
        if deduped:
            _notify_progress(settings=settings, token=token, progress_key="saving_cases", count=len(deduped))
            # Pass messages for evidence linking
            cases_inserted = _post_cases_to_bot(settings=settings, token=token, group_id=group_id, case_blocks=deduped, messages=msgs)
        else:
            log.info("No solved cases found in messages")

        _notify_link_result(
            settings=settings,
            token=token,
            success=True,
            message_count=len(msgs),
            cases_found=len(deduped),
            cases_inserted=cases_inserted,
        )
        
        # SECURITY: Reset Signal Desktop session after successful ingest
        # This ensures user's account is unlinked and requires new QR scan next time
        log.info("Resetting Signal Desktop session for security (unlinking user account)...")
        try:
            _reset_desktop(settings)
            log.info("Signal Desktop session reset successfully")
        except Exception as e:
            log.warning("Failed to reset Signal Desktop session: %s", e)

    except JobCancelled:
        log.info("Job %d was cancelled", job_id)
        # Also reset on cancellation for security
        try:
            _reset_desktop(settings)
        except Exception:
            pass
        raise


# ?????????????????????????????????????????????????????????????????????????????
# Main Loop
# ?????????????????????????????????????????????????????????????????????????????

def main() -> None:
    _configure_logging()
    settings = load_settings()
    db = create_db(settings)

    log.info("signal-ingest started (poll=%.2fs)", settings.worker_poll_seconds)
    
    if settings.use_signal_desktop:
        log.info("Mode: Signal Desktop (using already-linked instance at %s)", settings.signal_desktop_url)
    else:
        log.warning("Signal Desktop not enabled - history sync will not work")

    while True:
        job = claim_next_job(db, allowed_types=[HISTORY_LINK, HISTORY_SYNC])
        if job is None:
            time.sleep(settings.worker_poll_seconds)
            continue

        try:
            if job.type == HISTORY_LINK:
                if settings.use_signal_desktop:
                    _handle_history_link_desktop(settings=settings, db=db, job_id=job.job_id, payload=job.payload)
                else:
                    log.error("HISTORY_LINK job received but Signal Desktop is not enabled")
                    _notify_link_result(
                        settings=settings,
                        token=job.payload.get("token", ""),
                        success=False,
                        note="Signal Desktop is not enabled on server.",
                    )
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
