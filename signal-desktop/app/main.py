"""
Signal Desktop headless service.

This service runs Signal Desktop with Xvfb and provides an HTTP API to:
1. Check if Signal Desktop is linked/ready
2. Get the QR code for linking (via screenshot)
3. Poll for new messages from the SQLite database
4. Get messages for specific groups (for history ingestion)
5. Send messages to individuals and groups via Chrome DevTools Protocol
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional, List

import mimetypes

import httpx
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.config import load_settings
from app.db_reader import (
    SignalMessage as DBSignalMessage,
    get_attachment_stats,
    get_conversations,
    get_contacts_from_db,
    get_group_messages,
    get_messages,
    is_db_available,
    _open_db,
)
from app.devtools import DevToolsClient, get_devtools_client, SignalConversation
from app.cdn_download import download_and_decrypt, get_signal_credentials

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

app = FastAPI(title="Signal Desktop Service")
settings = load_settings()

# Track the last message timestamp we've seen (for polling)
_last_message_ts: int = 0
_last_message_ts_lock = asyncio.Lock()

# DevTools client (initialized on first use)
_devtools: Optional[DevToolsClient] = None
_devtools_lock = asyncio.Lock()


async def get_devtools() -> DevToolsClient:
    """Get or initialize the DevTools client."""
    global _devtools
    async with _devtools_lock:
        if _devtools is None or not _devtools.is_connected:
            _devtools = DevToolsClient(debug_port=9222)
            connected = await _devtools.connect()
            if connected:
                # Install message hook for receiving messages
                await _devtools.setup_message_hook()
            else:
                log.warning("Failed to connect to Signal Desktop DevTools")
        return _devtools


# ?????????????????????????????????????????????????????????????????????????????
# Health & Status
# ?????????????????????????????????????????????????????????????????????????????

@app.get("/healthz")
async def healthz():
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/status")
async def status():
    """
    Check Signal Desktop status.
    
    Returns:
        - linked: True if Signal Desktop has been linked to a user account
        - db_available: True if the SQLCipher DB can be opened
        - devtools_connected: True if DevTools connection is active
        - conversations_count: Number of conversations in DB
        - has_user_conversations: True if there are non-system conversations
    """
    db_available = is_db_available(settings.signal_data_dir)
    conversations_count = 0
    has_user_conversations = False
    
    if db_available:
        try:
            convs = get_conversations(settings.signal_data_dir)
            conversations_count = len(convs)
            # Check if there are any non-system conversations.
            # After QR scan the admin's "Note to Self" appears immediately (type=private, has e164/uuid).
            # Groups may take longer to sync.
            for c in convs:
                c_type = c.get("type") or ""
                if c_type == "group":
                    has_user_conversations = True
                    break
                if c_type == "private":
                    profile = c.get("profileName") or ""
                    e164 = c.get("e164") or ""
                    uuid = c.get("uuid") or ""
                    # Any private contact with a phone number or UUID that isn't the system Signal account
                    if e164 or uuid:
                        has_user_conversations = True
                        break
                    # Fall back to profileName check for older Signal Desktop versions
                    if profile and profile != "Signal":
                        has_user_conversations = True
                        break
        except Exception as e:
            log.warning("Failed to get conversations: %s", e)
    
    # Consider linked only when a real user account has synced conversations.
    # Signal Desktop creates 1 system conversation immediately on startup even
    # before any QR scan, so conversations_count > 0 is NOT a reliable linked
    # signal.  has_user_conversations (non-system private/group contacts) is
    # False until the admin scans the QR and their account data syncs.
    # signal-ingest polls for linked=False to know when the QR is visible.
    is_linked = has_user_conversations
    
    # Check DevTools connection
    devtools_connected = False
    try:
        devtools = await get_devtools()
        devtools_connected = devtools.is_connected
    except Exception:
        pass
    
    return {
        "linked": is_linked,
        "db_available": db_available,
        "devtools_connected": devtools_connected,
        "conversations_count": conversations_count,
        "has_user_conversations": has_user_conversations,
        "signal_data_dir": settings.signal_data_dir,
    }


# ?????????????????????????????????????????????????????????????????????????????
# Conversations & Messages (read from SQLite)
# ?????????????????????????????????????????????????????????????????????????????

@app.get("/conversations")
async def list_conversations():
    """List all conversations (groups and contacts)."""
    if not is_db_available(settings.signal_data_dir):
        raise HTTPException(status_code=503, detail="Signal Desktop not ready")
    
    try:
        convs = get_conversations(settings.signal_data_dir)
        return {"conversations": convs}
    except Exception as e:
        log.exception("Failed to get conversations")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/contacts")
async def list_contacts():
    """List contact identifiers from DB (no DevTools needed). For pruning when user removes bot."""
    if not is_db_available(settings.signal_data_dir):
        raise HTTPException(status_code=503, detail="Signal Desktop not ready")
    try:
        contacts = get_contacts_from_db(settings.signal_data_dir)
        return {"contacts": list(contacts)}
    except Exception as e:
        log.exception("Failed to get contacts")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/conversations/devtools")
async def list_conversations_devtools():
    """List all conversations via DevTools (alternative to SQLite)."""
    try:
        devtools = await get_devtools()
        if not devtools.is_connected:
            raise HTTPException(status_code=503, detail="DevTools not connected")
        
        convs = await devtools.list_conversations()
        return {
            "conversations": [
                {
                    "id": c.id,
                    "type": c.type,
                    "name": c.name,
                    "group_id": c.group_id,
                    "e164": c.e164,
                    "uuid": c.uuid,
                }
                for c in convs
            ]
        }
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Failed to get conversations via DevTools")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/messages")
async def poll_messages(
    since_ts: Optional[int] = Query(None, description="Get messages after this timestamp (ms)"),
    conversation_id: Optional[str] = Query(None, description="Filter to specific conversation"),
    limit: int = Query(100, description="Maximum messages to return"),
):
    """
    Poll for messages from Signal Desktop DB.
    
    This is the main endpoint for getting new messages.
    """
    if not is_db_available(settings.signal_data_dir):
        raise HTTPException(status_code=503, detail="Signal Desktop not ready")
    
    global _last_message_ts
    
    try:
        actual_since = since_ts
        if actual_since is None:
            async with _last_message_ts_lock:
                actual_since = _last_message_ts
        
        msgs = get_messages(
            signal_data_dir=settings.signal_data_dir,
            conversation_id=conversation_id,
            since_timestamp=actual_since,
            limit=limit,
        )
        
        if msgs:
            async with _last_message_ts_lock:
                max_ts = max(m.timestamp for m in msgs)
                if max_ts > _last_message_ts:
                    _last_message_ts = max_ts
        
        return {
            "messages": [
                {
                    "id": m.id,
                    "conversation_id": m.conversation_id,
                    "timestamp": m.timestamp,
                    "sender": m.sender,
                    "body": m.body,
                    "type": m.type,
                    "group_id": m.group_id,
                    "group_name": m.group_name,
                    "attachments": m.attachments,
                }
                for m in msgs
            ],
            "count": len(msgs),
            "last_ts": _last_message_ts,
        }
    except Exception as e:
        log.exception("Failed to poll messages")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/group/messages")
async def get_group_history(
    group_id: str = Query(..., description="Group ID"),
    group_name: Optional[str] = Query(None, description="Group name (fallback if ID not found)"),
    limit: int = Query(800, description="Maximum messages to return"),
):
    """
    Get all messages for a specific group.
    
    This is used for history ingestion - importing historical messages.
    """
    if not is_db_available(settings.signal_data_dir):
        raise HTTPException(status_code=503, detail="Signal Desktop not ready")
    
    try:
        msgs = get_group_messages(
            signal_data_dir=settings.signal_data_dir,
            group_id=group_id,
            group_name=group_name,
            limit=limit,
        )
        
        return {
            "messages": [
                {
                    "id": m.id,
                    "conversation_id": m.conversation_id,
                    "timestamp": m.timestamp,
                    "sender": m.sender,
                    "sender_name": m.sender_name,
                    "body": m.body,
                    "type": m.type,
                    "group_id": m.group_id,
                    "group_name": m.group_name,
                    "reactions": m.reactions,
                    "reaction_emoji": m.reaction_emoji,
                    "attachments": m.attachments,
                }
                for m in msgs
            ],
            "count": len(msgs),
            "group_id": group_id,
            "group_name": group_name,
        }
    except Exception as e:
        log.exception("Failed to get group messages")
        raise HTTPException(status_code=500, detail=str(e))


# ?????????????????????????????????????????????????????????????????????????????
# Send Messages (via DevTools)
# ?????????????????????????????????????????????????????????????????????????????

class SendMessageRequest(BaseModel):
    """Request body for sending a message."""
    recipient: str = Field(..., description="Phone number in E.164 format (e.g., +12345678910)")
    text: str = Field(..., description="Message text to send")
    expire_timer: int = Field(0, description="Disappearing message timer in seconds (0 = no expiration)")


class SendGroupMessageRequest(BaseModel):
    """Request body for sending a group message."""
    group_id: str = Field(..., description="Group ID or group name")
    text: str = Field(..., description="Message text to send")
    expire_timer: int = Field(0, description="Disappearing message timer in seconds (0 = no expiration)")


class SendImageRequest(BaseModel):
    """Request body for sending an image."""
    recipient: str = Field(..., description="Phone number in E.164 format")
    image_base64: str = Field(..., description="Base64-encoded PNG image data")
    caption: str = Field("", description="Optional caption for the image")


@app.post("/send")
async def send_message(request: SendMessageRequest):
    """
    Send a text message to a Signal user.
    
    Uses Chrome DevTools Protocol to automate Signal Desktop.
    """
    try:
        devtools = await get_devtools()
        if not devtools.is_connected:
            raise HTTPException(status_code=503, detail="DevTools not connected to Signal Desktop")
        
        success = await devtools.send_message(
            recipient=request.recipient,
            text=request.text,
            expire_timer=request.expire_timer,
        )
        
        if success:
            return {"success": True, "message": "Message sent"}
        else:
            raise HTTPException(status_code=500, detail="Failed to send message")
            
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Failed to send message")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/send/group")
async def send_group_message(request: SendGroupMessageRequest):
    """
    Send a text message to a Signal group.
    
    Uses Chrome DevTools Protocol to automate Signal Desktop.
    """
    try:
        devtools = await get_devtools()
        if not devtools.is_connected:
            raise HTTPException(status_code=503, detail="DevTools not connected to Signal Desktop")
        
        success = await devtools.send_group_message(
            group_id=request.group_id,
            text=request.text,
            expire_timer=request.expire_timer,
        )
        
        if success:
            return {"success": True, "message": "Group message sent"}
        else:
            raise HTTPException(status_code=500, detail="Failed to send group message")
            
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Failed to send group message")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/send/image")
async def send_image(request: SendImageRequest):
    """
    Send an image to a Signal user via Chrome DevTools Protocol.

    The image is supplied as a base64-encoded string so no shared filesystem
    between containers is required.
    """
    try:
        devtools = await get_devtools()
        if not devtools.is_connected:
            raise HTTPException(status_code=503, detail="DevTools not connected to Signal Desktop")

        success = await devtools.send_image(
            recipient=request.recipient,
            image_base64=request.image_base64,
            caption=request.caption,
        )

        if success:
            return {"success": True, "message": "Image sent"}
        else:
            raise HTTPException(status_code=500, detail="Failed to send image via DevTools")
            
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Failed to send image")
        raise HTTPException(status_code=500, detail=str(e))


# ?????????????????????????????????????????????????????????????????????????????
# Groups
# ?????????????????????????????????????????????????????????????????????????????

@app.get("/groups")
async def list_groups():
    """List all groups the user is a member of."""
    try:
        devtools = await get_devtools()
        if not devtools.is_connected:
            raise HTTPException(status_code=503, detail="DevTools not connected")
        
        convs = await devtools.list_conversations()
        groups = [c for c in convs if c.type == "group"]
        
        return {
            "groups": [
                {
                    "id": g.id,
                    "name": g.name,
                    "group_id": g.group_id,
                }
                for g in groups
            ]
        }
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Failed to list groups")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/groups/find")
async def find_group(name: str = Query(..., description="Group name to search for")):
    """Find a group by name (case-insensitive partial match)."""
    try:
        devtools = await get_devtools()
        if not devtools.is_connected:
            raise HTTPException(status_code=503, detail="DevTools not connected")
        
        group = await devtools.find_group_by_name(name)
        
        if group:
            return {
                "found": True,
                "group": {
                    "id": group.id,
                    "name": group.name,
                    "group_id": group.group_id,
                }
            }
        else:
            return {"found": False, "group": None}
            
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Failed to find group")
        raise HTTPException(status_code=500, detail=str(e))


# ?????????????????????????????????????????????????????????????????????????????
# Screenshot & Reset
# ?????????????????????????????????????????????????????????????????????????????

@app.get("/screenshot")
async def take_screenshot(crop_qr: bool = Query(True, description="Crop to just the QR code area")):
    """
    Take a screenshot of Signal Desktop (for QR code during linking).
    
    By default, crops to just the QR code area so users can scan directly.
    Set crop_qr=false to get the full screenshot.
    """
    try:
        # Take screenshot using xwd + convert
        result = subprocess.run(
            ["xwd", "-root", "-display", ":99"],
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail="Failed to capture screenshot")
        
        if crop_qr:
            # Crop to QR code area (Signal Desktop QR is roughly in center-left)
            # QR code is at approximately x=[163, 418], y=[270, 525]
            # Center is (290, 397)
            # Crop a 300x300 area centered on the QR
            # Crop: 300x300 starting at (140, 247)
            # -threshold 50%: forces pure black/white so Signal's gray QR scans reliably
            convert_result = subprocess.run(
                ["convert", "xwd:-", "-crop", "300x300+140+247", "+repage",
                 "-threshold", "50%", "png:-"],
                input=result.stdout,
                capture_output=True,
                timeout=10,
            )
        else:
            # Convert XWD to PNG without cropping
            convert_result = subprocess.run(
                ["convert", "xwd:-", "png:-"],
                input=result.stdout,
                capture_output=True,
                timeout=10,
            )
        
        if convert_result.returncode != 0:
            raise HTTPException(status_code=500, detail="Failed to convert screenshot")
        
        return Response(content=convert_result.stdout, media_type="image/png")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="Screenshot timed out")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="xwd or convert not installed")
    except Exception as e:
        log.exception("Screenshot failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/attachment")
async def get_attachment(path: str = Query(..., description="Relative attachment path from Signal data dir")):
    """
    Serve a Signal Desktop attachment by its relative path.

    Attachment paths come from the ``path`` field in the ``attachments`` array
    of a message's JSON column (e.g. ``attachments.noindex/abc123/image.jpg``).

    Access is restricted to files inside the Signal data directory to prevent
    directory traversal attacks.
    """
    data_dir = Path(settings.signal_data_dir).resolve()
    # Normalise the path and prevent traversal outside the data dir
    try:
        full_path = (data_dir / path).resolve()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid path: {e}")

    if not str(full_path).startswith(str(data_dir)):
        raise HTTPException(status_code=403, detail="Path traversal not allowed")

    if not full_path.exists() or not full_path.is_file():
        raise HTTPException(status_code=404, detail=f"Attachment not found: {path}")

    mime_type, _ = mimetypes.guess_type(str(full_path))
    content = full_path.read_bytes()
    return Response(content=content, media_type=mime_type or "application/octet-stream")


@app.get("/attachment/by-cdn/{cdn_key:path}")
async def get_attachment_by_cdn(cdn_key: str):
    """
    Serve a Signal attachment that was downloaded directly from the CDN.

    Files are cached at ``{signal_data_dir}/cdn-cache/{cdn_key}`` by the
    ``POST /attachments/fetch-all`` endpoint.  Returns 404 if the file has
    not been downloaded yet.
    """
    cache_file = Path(settings.signal_data_dir) / "cdn-cache" / cdn_key
    if not cache_file.exists():
        raise HTTPException(status_code=404, detail=f"CDN-cached attachment not found: {cdn_key}")
    mime_type, _ = mimetypes.guess_type(str(cache_file))
    return Response(
        content=cache_file.read_bytes(),
        media_type=mime_type or "application/octet-stream",
    )


@app.post("/attachments/fetch-all")
async def fetch_all_pending_attachments(
    group_id: Optional[str] = Query(None, description="Signal group ID"),
    group_name: Optional[str] = Query(None, description="Group name (fallback)"),
):
    """
    Download all pending attachments for a group directly from Signal's CDN.

    For every attachment in the group's messages that has CDN metadata
    (``cdnKey``, ``key``) but no local ``path`` yet, this endpoint:

    1. Reads account credentials from the Signal Desktop SQLite ``items`` table.
    2. Issues a GET to ``cdn{N}.signal.org`` with Basic auth.
    3. Decrypts the blob (AES-256-CBC + HMAC-SHA256).
    4. Caches the plaintext at ``{signal_data_dir}/cdn-cache/{cdnKey}``.

    Files already cached are skipped.  Returns ``{downloaded, failed, skipped}``.
    """
    if not is_db_available(settings.signal_data_dir):
        return {"downloaded": 0, "failed": 0, "skipped": 0, "error": "DB not available"}

    # Resolve conversation_id
    conversation_id: Optional[str] = None
    try:
        convs = get_conversations(settings.signal_data_dir)
        group_name_lower = (group_name or "").lower().strip()
        for c in convs:
            if group_id and (c.get("groupId") == group_id or c.get("id") == group_id):
                conversation_id = c["id"]
                break
            if group_name_lower and (c.get("name") or "").lower().strip() == group_name_lower:
                conversation_id = c["id"]
                break
    except Exception as e:
        log.warning("fetch-all: could not resolve conversation: %s", e)

    # Collect all pending attachments
    import json as _json

    pending: list[dict] = []
    skip_no_cdn = 0
    skip_cached = 0
    skip_on_disk = 0
    total_att_entries = 0
    try:
        conn = _open_db(settings.signal_data_dir)

        # Use a broader pattern to catch all non-empty attachment arrays,
        # including `[{`, `[ {`, `[null`, etc.
        q_broad = "SELECT COUNT(*) FROM messages WHERE json LIKE '%\"attachments\":[%' AND json NOT LIKE '%\"attachments\":[]%'"
        if conversation_id:
            q_broad += " AND conversationId = ?"
            broad_count = conn.execute(q_broad, [conversation_id]).fetchone()[0]
        else:
            broad_count = conn.execute(q_broad).fetchone()[0]
        log.info(
            "fetch-all: group=%s conv_id=%s — messages with non-empty attachments array: %d",
            group_name or group_id,
            conversation_id or "(all groups)",
            broad_count,
        )

        # Use broad pattern to catch all non-empty attachment arrays, then
        # filter in Python (handles `[{`, `[ {`, `[null,{` etc.)
        q = "SELECT json FROM messages WHERE json LIKE '%\"attachments\":[%' AND json NOT LIKE '%\"attachments\":[]%'"
        params: list = []
        if conversation_id:
            q += " AND conversationId = ?"
            params.append(conversation_id)
        rows = conn.execute(q, params).fetchall()
        log.info("fetch-all: messages with non-empty attachments (broad pattern): %d", len(rows))
        conn.close()

        for (raw_json,) in rows:
            try:
                msg = _json.loads(raw_json) if raw_json else {}
                for att in msg.get("attachments") or []:
                    if not isinstance(att, dict):
                        continue
                    total_att_entries += 1
                    cdn_key = att.get("cdnKey") or ""
                    key_b64 = att.get("key") or ""
                    if not cdn_key or not key_b64:
                        skip_no_cdn += 1
                        if skip_no_cdn <= 3:
                            log.info(
                                "fetch-all: skip (no cdnKey/key): contentType=%s cdnKey=%r key_len=%d path=%r",
                                att.get("contentType"), cdn_key, len(key_b64), att.get("path"),
                            )
                        continue
                    # Skip if already cached or already on disk
                    cache_file = Path(settings.signal_data_dir) / "cdn-cache" / cdn_key
                    if cache_file.exists():
                        skip_cached += 1
                        continue
                    if att.get("path"):
                        skip_on_disk += 1
                        continue
                    pending.append({
                        "cdnKey": cdn_key,
                        "cdnNumber": att.get("cdnNumber"),
                        "key": key_b64,
                        "contentType": att.get("contentType") or "",
                    })
            except Exception:
                continue
    except Exception as e:
        log.exception("fetch-all: failed to scan messages")
        return {"downloaded": 0, "failed": 0, "skipped": 0, "error": str(e)}

    log.info(
        "fetch-all: total_att=%d pending=%d skip_no_cdn=%d skip_cached=%d skip_on_disk=%d",
        total_att_entries, len(pending), skip_no_cdn, skip_cached, skip_on_disk,
    )
    if not pending:
        log.info("fetch-all: no pending attachments for group %s", group_name or group_id)
        return {"downloaded": 0, "failed": 0, "skipped": 0}

    # Load credentials once
    credentials: dict = {}
    try:
        conn = _open_db(settings.signal_data_dir)
        credentials = get_signal_credentials(conn)
        conn.close()
        log.info(
            "fetch-all: CDN credentials — uuid=%s... deviceId=%s",
            (credentials.get("uuid") or "")[:8],
            credentials.get("deviceId"),
        )
    except Exception as e:
        log.warning("fetch-all: could not read credentials: %s", e)

    downloaded = 0
    failed = 0
    skipped = 0

    log.info("fetch-all: downloading %d pending attachment(s) for group %s", len(pending), group_name or group_id)
    for att in pending:
        cdn_key = att["cdnKey"]
        try:
            download_and_decrypt(
                cdn_key=cdn_key,
                cdn_number=att.get("cdnNumber"),
                key_b64=att["key"],
                signal_data_dir=settings.signal_data_dir,
                credentials=credentials,
            )
            downloaded += 1
            log.debug("fetch-all: downloaded %s", cdn_key)
        except Exception as e:
            failed += 1
            log.warning("fetch-all: failed to download %s: %s", cdn_key, e)

    log.info(
        "fetch-all: done — downloaded=%d failed=%d skipped=%d",
        downloaded, failed, skipped,
    )
    return {"downloaded": downloaded, "failed": failed, "skipped": skipped}


@app.get("/attachments/files")
async def count_attachment_files():
    """
    Count files currently in Signal Desktop's ``attachments.noindex`` directory.

    Signal Desktop writes attachment files here as they finish downloading.
    Polling this count (instead of the SQLite DB) avoids issues where the DB
    attachment metadata shows ``size=null`` or missing ``cdnKey`` for pending
    downloads.  When the count stops increasing, all in-flight downloads have
    finished.

    Response: ``{count, dir_exists}``
    """
    att_dir = Path(settings.signal_data_dir) / "attachments.noindex"
    if not att_dir.exists():
        return {"count": 0, "dir_exists": False}
    try:
        count = sum(1 for _ in att_dir.rglob("*") if _.is_file())
        return {"count": count, "dir_exists": True}
    except Exception as e:
        log.warning("Failed to count attachment files: %s", e)
        return {"count": 0, "dir_exists": False, "error": str(e)}


@app.post("/attachments/trigger")
async def trigger_attachments(
    group_id: str = Query(..., description="Signal group ID"),
    group_name: Optional[str] = Query(None, description="Group name (for logging)"),
):
    """
    Ask Signal Desktop's JS runtime to enqueue pending attachment downloads.

    Signal Desktop does not auto-download historical attachment files in headless
    mode.  This endpoint injects a small JS snippet via CDP that calls the
    internal ``AttachmentDownloads.addJob()`` service for every attachment that
    has CDN metadata but no local ``path`` yet.

    Call this once after QR-link + group sync, then poll ``/attachments/status``
    until ``pending == 0``.

    Response: ``{ok, triggered, method}``
    """
    try:
        devtools = await get_devtools()
        if not devtools.is_connected:
            return {"ok": False, "error": "DevTools not connected", "triggered": 0}
        result = await devtools.trigger_attachment_downloads(group_id, group_name=group_name or "")
        log.info(
            "Attachment trigger: group=%s result=%s",
            (group_name or group_id)[:30], result,
        )
        return result
    except Exception as e:
        log.exception("trigger_attachments failed")
        return {"ok": False, "error": str(e), "triggered": 0}


@app.get("/attachments/status")
async def get_attachments_status(
    group_id: Optional[str] = Query(None, description="Signal group ID"),
    group_name: Optional[str] = Query(None, description="Group name (fallback)"),
):
    """
    Return attachment download progress for a group.

    Signal Desktop downloads attachment files in the background after linking.
    Poll this endpoint to know when all attachment files are on disk (``pending == 0``).

    Response fields:
    - ``total_attachments``: total media attachments found in message JSON
    - ``ready``: attachments with a local file path (downloaded)
    - ``pending``: attachments with CDN metadata but no local path yet
    - ``db_available``: whether the Signal Desktop DB could be opened
    """
    db_ok = is_db_available(settings.signal_data_dir)
    if not db_ok:
        return {"total_attachments": 0, "ready": 0, "pending": 0, "db_available": False}

    # Resolve conversation_id from group_id or group_name
    conversation_id: Optional[str] = None
    if group_id or group_name:
        try:
            convs = get_conversations(settings.signal_data_dir)
            group_name_lower = (group_name or "").lower().strip()
            for c in convs:
                if group_id and (c.get("groupId") == group_id or c.get("id") == group_id):
                    conversation_id = c["id"]
                    break
                if group_name_lower and (c.get("name") or "").lower().strip() == group_name_lower:
                    conversation_id = c["id"]
                    break
        except Exception as e:
            log.warning("Could not resolve conversation for attachment status: %s", e)

    try:
        stats = get_attachment_stats(settings.signal_data_dir, conversation_id)
        return {**stats, "db_available": True, "conversation_id": conversation_id}
    except Exception as e:
        log.exception("Failed to get attachment stats")
        return {"total_attachments": 0, "ready": 0, "pending": 0, "db_available": True, "error": str(e)}


@app.post("/refresh-qr")
async def refresh_qr():
    """
    Click the 'Refresh code' button in Signal Desktop to regenerate an expired QR code,
    then return a fresh cropped screenshot.

    Call this when the QR code has expired (screenshot is blank / user couldn't scan).
    Signal Desktop automatically shows a 'Refresh code' button when the QR expires.
    """
    try:
        devtools = await get_devtools()
        if devtools.is_connected:
            # Click any button whose visible text contains 'Refresh' (case-insensitive)
            js = (
                "(function(){"
                "  var btns = Array.from(document.querySelectorAll('button'));"
                "  var btn = btns.find(function(b){"
                "    return (b.innerText||b.textContent||'').toLowerCase().includes('refresh');"
                "  });"
                "  if(btn){ btn.click(); return 'clicked'; }"
                "  return 'not_found';"
                "})()"
            )
            result = await devtools._send_command(
                "Runtime.evaluate", {"expression": js, "returnByValue": True}
            )
            clicked = (result or {}).get("result", {}).get("value", "unknown")
            log.info("Refresh QR click result: %s", clicked)
        else:
            log.warning("DevTools not connected; cannot click Refresh button")

        # Wait for Signal Desktop to generate and render the new QR code
        await asyncio.sleep(6)

        # Take a fresh cropped screenshot and return it
        result_sc = subprocess.run(
            ["xwd", "-root", "-display", ":99"],
            capture_output=True, timeout=10,
        )
        if result_sc.returncode != 0:
            raise HTTPException(status_code=500, detail="Screenshot failed after QR refresh")

        convert_result = subprocess.run(
            ["convert", "xwd:-", "-crop", "300x300+140+247", "+repage",
             "-threshold", "50%", "png:-"],
            input=result_sc.stdout, capture_output=True, timeout=10,
        )
        if convert_result.returncode != 0:
            raise HTTPException(status_code=500, detail="Convert failed after QR refresh")

        return Response(content=convert_result.stdout, media_type="image/png")

    except HTTPException:
        raise
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="Screenshot timed out")
    except Exception as e:
        log.exception("refresh-qr failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/reset-poll-timestamp")
async def reset_poll_timestamp(ts: int = Query(0, description="New timestamp to start from")):
    """Reset the poll timestamp (useful for re-ingesting history)."""
    global _last_message_ts
    async with _last_message_ts_lock:
        _last_message_ts = ts
    return {"last_ts": _last_message_ts}


@app.get("/attachments/diagnose")
async def diagnose_attachments(
    group_id: Optional[str] = Query(None, description="Signal group ID"),
    group_name: Optional[str] = Query(None, description="Group name (fallback)"),
):
    """
    Diagnose attachment data structure in Signal Desktop DB for a group.

    Returns a breakdown of how many messages have attachments, what fields
    are present (cdnKey, key, path, contentType), and sample attachment JSON.
    Useful for debugging why fetch-all reports 0 attachments.
    """
    if not is_db_available(settings.signal_data_dir):
        return {"error": "DB not available"}

    import json as _json

    conversation_id: Optional[str] = None
    if group_id or group_name:
        try:
            convs = get_conversations(settings.signal_data_dir)
            group_name_lower = (group_name or "").lower().strip()
            for c in convs:
                if group_id and (c.get("groupId") == group_id or c.get("id") == group_id):
                    conversation_id = c["id"]
                    break
                if group_name_lower and (c.get("name") or "").lower().strip() == group_name_lower:
                    conversation_id = c["id"]
                    break
        except Exception as e:
            return {"error": f"Could not resolve conversation: {e}"}

    try:
        conn = _open_db(settings.signal_data_dir)

        # Count messages with any attachment data
        base_q = "FROM messages WHERE json LIKE '%\"attachments\":%'"
        conv_filter = ""
        params: list = []
        if conversation_id:
            conv_filter = " AND conversationId = ?"
            params = [conversation_id]

        total_msgs = conn.execute(f"SELECT COUNT(*) {base_q}{conv_filter}", params).fetchone()[0]
        non_empty = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE json LIKE '%\"attachments\":[%' "
            f"AND json NOT LIKE '%\"attachments\":[]%'{conv_filter}",
            params
        ).fetchone()[0]
        strict_pattern = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE json LIKE '%\"attachments\":[{{%'{conv_filter}",
            params
        ).fetchone()[0]

        # Sample the actual attachment JSON
        sample_rows = conn.execute(
            f"SELECT json FROM messages WHERE json LIKE '%\"attachments\":[%' "
            f"AND json NOT LIKE '%\"attachments\":[]%'{conv_filter} LIMIT 5",
            params
        ).fetchall()

        samples = []
        has_cdn_key = 0
        has_key_b64 = 0
        has_path = 0
        has_content_type_only = 0
        total_att = 0

        for (raw,) in sample_rows:
            try:
                msg = _json.loads(raw) if raw else {}
                atts = msg.get("attachments") or []
                for att in atts:
                    if not isinstance(att, dict):
                        continue
                    total_att += 1
                    if att.get("cdnKey"):
                        has_cdn_key += 1
                    if att.get("key"):
                        has_key_b64 += 1
                    if att.get("path"):
                        has_path += 1
                    if att.get("contentType") and not att.get("cdnKey") and not att.get("path"):
                        has_content_type_only += 1
                if len(samples) < 3:
                    # Show raw types of items (null vs dict) and first dict's fields
                    item_types = [type(a).__name__ for a in atts[:4]]
                    first_dict = next((a for a in atts if isinstance(a, dict)), {})
                    samples.append({
                        "count": len(atts),
                        "item_types": item_types,
                        "fields": [list(a.keys()) for a in atts[:2] if isinstance(a, dict)],
                        "first_att": {
                            k: (v[:30] if isinstance(v, str) else v)
                            for k, v in first_dict.items()
                            if k in ("contentType", "cdnKey", "cdnNumber", "path", "fileName", "size")
                        },
                        # Show raw JSON snippet around the attachments array for debugging
                        "raw_snippet": (raw or "")[(raw or "").find('"attachments":'):(raw or "").find('"attachments":') + 200] if raw else "",
                    })
            except Exception:
                pass

        conn.close()

        return {
            "conversation_id": conversation_id,
            "messages_with_any_attachments_key": total_msgs,
            "messages_with_non_empty_attachments": non_empty,
            "messages_with_strict_bracket_pattern": strict_pattern,
            "sample_attachments_breakdown": {
                "total_att_entries_in_samples": total_att,
                "has_cdn_key": has_cdn_key,
                "has_decryption_key": has_key_b64,
                "has_path_on_disk": has_path,
                "has_content_type_only": has_content_type_only,
            },
            "samples": samples,
        }
    except Exception as e:
        log.exception("diagnose_attachments failed")
        return {"error": str(e)}


@app.post("/reset")
async def reset_signal_desktop():
    """
    Reset Signal Desktop for a new linking session.
    
    This clears the Signal data directory and restarts Signal Desktop,
    causing it to show the QR code for linking.
    """
    import shutil
    
    signal_data_dir = Path(settings.signal_data_dir)
    
    log.info("Resetting Signal Desktop - clearing data directory: %s", signal_data_dir)
    
    # Disconnect DevTools
    global _devtools
    if _devtools:
        await _devtools.disconnect()
        _devtools = None
    
    # Kill Signal Desktop process
    try:
        result = subprocess.run(["pkill", "-f", "signal-desktop"], timeout=5)
        log.info("Killed Signal Desktop process (rc=%d)", result.returncode)
    except Exception as e:
        log.warning("Failed to kill Signal Desktop: %s", e)
    
    await asyncio.sleep(2)
    
    # Clear data directory
    try:
        if signal_data_dir.exists():
            for item in signal_data_dir.iterdir():
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            log.info("Cleared Signal data directory")
    except Exception as e:
        log.exception("Failed to clear Signal data directory")
        raise HTTPException(status_code=500, detail=f"Failed to clear data: {e}")
    
    # Restart Signal Desktop with remote debugging
    try:
        subprocess.Popen(
            ["signal-desktop", "--no-sandbox", "--disable-gpu", "--remote-debugging-port=9222"],
            env={**os.environ, "DISPLAY": ":99"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info("Started Signal Desktop with remote debugging")
    except Exception as e:
        log.exception("Failed to start Signal Desktop")
        raise HTTPException(status_code=500, detail=f"Failed to start Signal Desktop: {e}")
    
    await asyncio.sleep(5)
    
    return {"status": "reset", "message": "Signal Desktop reset. Use /screenshot to get QR code."}


# ?????????????????????????????????????????????????????????????????????????????
# DevTools Management
# ?????????????????????????????????????????????????????????????????????????????

@app.post("/devtools/connect")
async def connect_devtools():
    """Manually connect to Signal Desktop DevTools."""
    try:
        devtools = await get_devtools()
        if devtools.is_connected:
            return {"connected": True, "message": "Connected to DevTools"}
        else:
            return {"connected": False, "message": "Failed to connect to DevTools"}
    except Exception as e:
        log.exception("Failed to connect to DevTools")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/devtools/disconnect")
async def disconnect_devtools():
    """Disconnect from Signal Desktop DevTools."""
    global _devtools
    if _devtools:
        await _devtools.disconnect()
        _devtools = None
    return {"connected": False, "message": "Disconnected from DevTools"}


@app.get("/devtools/status")
async def devtools_status():
    """Check DevTools connection status."""
    global _devtools
    if _devtools and _devtools.is_connected:
        return {"connected": True}
    return {"connected": False}
