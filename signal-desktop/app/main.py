"""
Signal Desktop headless service.

This service runs Signal Desktop with Xvfb and provides an HTTP API to:
1. Check if Signal Desktop is linked/ready
2. Get the QR code for linking (via VNC or screenshot)
3. Poll for new messages from the SQLite database
4. Get messages for specific groups (for history ingestion)
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
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response

from app.config import load_settings
from app.db_reader import (
    SignalMessage,
    get_conversations,
    get_group_messages,
    get_messages,
    is_db_available,
)

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
            # Check if there are any non-system conversations
            # Signal creates a "Signal" system conversation immediately,
            # but real user conversations only appear after linking
            for c in convs:
                # Groups are definitely user conversations
                if c.get("type") == "group":
                    has_user_conversations = True
                    break
                # Private conversations with a profileName other than "Signal" are user convos
                profile = c.get("profileName") or ""
                if c.get("type") == "private" and profile and profile != "Signal":
                    has_user_conversations = True
                    break
        except Exception as e:
            log.warning("Failed to get conversations: %s", e)
    
    # Consider linked only if we have actual user conversations
    # (not just the Signal system conversation)
    is_linked = db_available and has_user_conversations
    
    return {
        "linked": is_linked,
        "db_available": db_available,
        "conversations_count": conversations_count,
        "has_user_conversations": has_user_conversations,
        "signal_data_dir": settings.signal_data_dir,
    }


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
        # Use the tracked timestamp if not provided
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
        
        # Update last seen timestamp
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
                }
                for m in msgs
            ],
            "count": len(msgs),
            "last_ts": _last_message_ts,
        }
    except Exception as e:
        log.exception("Failed to poll messages")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/group/{group_id}/messages")
async def get_group_history(
    group_id: str,
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
                    "body": m.body,
                    "type": m.type,
                    "group_id": m.group_id,
                    "group_name": m.group_name,
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


@app.get("/screenshot")
async def take_screenshot(crop_qr: bool = Query(True, description="Crop to just the QR code area")):
    """
    Take a screenshot of Signal Desktop (for QR code during linking).
    
    By default, crops to just the QR code area so users can scan directly.
    Set crop_qr=false to get the full screenshot.
    
    This requires xwd and ImageMagick to be installed.
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
            # Adjusted based on feedback: moved down and slightly larger
            convert_result = subprocess.run(
                ["convert", "xwd:-", "-crop", "340x340+105+210", "+repage", "png:-"],
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
    
    Used for history ingestion: user scans QR → Signal Desktop links to their account
    → syncs their 45-day history → we read it.
    """
    import shutil
    import signal as sig
    
    signal_data_dir = Path(settings.signal_data_dir)
    
    log.info("Resetting Signal Desktop - clearing data directory: %s", signal_data_dir)
    
    # Kill Signal Desktop process
    try:
        result = subprocess.run(["pkill", "-f", "signal-desktop"], timeout=5)
        log.info("Killed Signal Desktop process (rc=%d)", result.returncode)
    except Exception as e:
        log.warning("Failed to kill Signal Desktop: %s", e)
    
    # Wait a moment for process to die
    await asyncio.sleep(2)
    
    # Clear data directory (keep the directory itself)
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
    
    # Restart Signal Desktop (it should show linking QR on startup)
    try:
        subprocess.Popen(
            ["signal-desktop", "--no-sandbox", "--disable-gpu"],
            env={**os.environ, "DISPLAY": ":99"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info("Started Signal Desktop")
    except Exception as e:
        log.exception("Failed to start Signal Desktop")
        raise HTTPException(status_code=500, detail=f"Failed to start Signal Desktop: {e}")
    
    # Wait for it to start and show QR
    await asyncio.sleep(5)
    
    return {"status": "reset", "message": "Signal Desktop reset. Use /screenshot to get QR code."}
