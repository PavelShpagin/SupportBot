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
    get_conversations,
    get_contacts_from_db,
    get_group_messages,
    get_messages,
    is_db_available,
)
from app.devtools import DevToolsClient, get_devtools_client, SignalConversation

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


@app.post("/reset-poll-timestamp")
async def reset_poll_timestamp(ts: int = Query(0, description="New timestamp to start from")):
    """Reset the poll timestamp (useful for re-ingesting history)."""
    global _last_message_ts
    async with _last_message_ts_lock:
        _last_message_ts = ts
    return {"last_ts": _last_message_ts}


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
