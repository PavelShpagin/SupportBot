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
    get_all_local_encrypted_attachments,
    get_conversations,
    get_contacts_from_db,
    get_group_messages,
    get_local_key_for_path,
    get_messages,
    is_db_available,
    _get_all_tables,
    _get_table_columns,
    _open_db,
)
from app.devtools import DevToolsClient, get_devtools_client, SignalConversation
from app.cdn_download import decrypt_local_attachment, download_and_decrypt, get_signal_credentials

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

app = FastAPI(title="Signal Desktop Service")
settings = load_settings()

_ATTACH_SUBDIR = "attachments.noindex"


def _resolve_attachment_path(data_dir: str, rel_path: str) -> Optional[Path]:
    """Resolve a DB attachment path to its full filesystem path.

    Signal Desktop stores paths in the DB relative to ``attachments.noindex/``
    (e.g. ``5e/5e62df3c…``).  This helper tries both ``<data_dir>/<rel_path>``
    and ``<data_dir>/attachments.noindex/<rel_path>`` so callers don't need to
    care which convention the incoming path uses.
    """
    base = Path(data_dir)
    candidate = (base / rel_path).resolve()
    if candidate.is_file():
        return candidate
    candidate = (base / _ATTACH_SUBDIR / rel_path).resolve()
    if candidate.is_file():
        return candidate
    return None


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
                    "quote_id": m.quote_id,
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
                    "quote_id": m.quote_id,
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
                    "description": g.description,
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
                    "description": group.description,
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
            # Crop: 300x300 starting at (140, 247), then upscale to 800x800.
            # -threshold 50%: forces pure black/white so Signal's gray QR scans reliably
            # -sample 800x800: nearest-neighbor upscale (no interpolation blur)
            # -depth 8 -type Grayscale: 8-bit grayscale so it survives Signal's
            #   JPEG recompression without destroying sharp QR module edges
            convert_result = subprocess.run(
                ["convert", "xwd:-", "-crop", "300x300+140+247", "+repage",
                 "-resize", "800x800", "-threshold", "50%",
                 "-depth", "8", "png:-"],
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


@app.get("/qr-png")
async def get_qr_png():
    """Extract the sgnl:// linking URL from Signal Desktop via DevTools and
    return a clean, regenerated QR code PNG (800x800).

    This avoids screenshot artifacts that make QR codes unscannable after
    Signal's JPEG recompression when sent as attachments.
    """
    import qrcode

    devtools = await get_devtools()
    if not devtools.is_connected:
        raise HTTPException(status_code=503, detail="DevTools not connected")

    # Extract sgnl:// URL from Signal Desktop's React/Redux state.
    js = """
    (function(){
        // Approach 1: Signal stores provisioning URL in window.Signal Redux store
        try {
            var state = window.reduxStore && window.reduxStore.getState();
            if (state) {
                // Check various possible paths in the state tree
                var url = (state.installer && state.installer.provisioningUrl)
                    || (state.app && state.app.provisioningUrl);
                if (url) return url;
            }
        } catch(e) {}

        // Approach 2: Look for SignalContext or other Electron-exposed globals
        try {
            var keys = Object.keys(window).filter(function(k) {
                return k.toLowerCase().includes('signal') || k.toLowerCase().includes('provision');
            });
            if (keys.length > 0) return 'KEYS:' + keys.join(',');
        } catch(e) {}

        // Approach 3: Check for SVG-based QR (Signal renders QR as SVG)
        var svgs = document.querySelectorAll('svg');
        if (svgs.length > 0) {
            var svg = svgs[0];
            // Return first 500 chars for debugging plus viewBox and style info
            var info = 'viewBox=' + svg.getAttribute('viewBox')
                + ' width=' + svg.getAttribute('width')
                + ' height=' + svg.getAttribute('height')
                + ' fill=' + svg.getAttribute('fill')
                + ' style=' + (svg.style.cssText || 'none')
                + ' class=' + (svg.className.baseVal || 'none');
            // Get first path to see fill color
            var paths = svg.querySelectorAll('path');
            if (paths.length > 0) {
                info += ' path0_fill=' + paths[0].getAttribute('fill')
                    + ' path0_d=' + (paths[0].getAttribute('d') || '').substring(0, 100);
            }
            return 'SVG_INFO:' + info;
        }

        // Approach 4: Look for data: URL images
        var imgs = document.querySelectorAll('img[src^="data:"]');
        if (imgs.length > 0) {
            return 'DATA_IMG:' + imgs[0].src.substring(0, 300);
        }

        // Approach 5: Dump interesting DOM elements for debugging
        var qrEl = document.querySelector('[class*="qr" i], [class*="QR"], [data-testid*="qr" i]');
        if (qrEl) return 'QR_EL:' + qrEl.outerHTML.substring(0, 500);

        // Approach 6: Search full HTML
        var all = document.documentElement.outerHTML;
        var m = all.match(/sgnl:\\/\\/linkdevice\\?[^"'<>\\s]+/);
        if (m) return m[0];

        return 'NO_MATCH:DOM_LEN=' + all.length;
    })()
    """
    result = await devtools._send_command(
        "Runtime.evaluate", {"expression": js, "returnByValue": True}
    )
    raw = (result or {}).get("result", {}).get("value") or ""

    if raw.startswith("sgnl://"):
        # Got the URL directly — regenerate clean QR
        log.info("Extracted QR URL: %s", raw[:60])
        import qrcode
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=20, border=4)
        qr.add_data(raw)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Response(content=buf.getvalue(), media_type="image/png")

    if raw.startswith("SVG_INFO:"):
        raise HTTPException(status_code=404, detail=raw[:500])

    if raw.startswith("SVG:"):
        # Got the SVG QR — convert to high-quality PNG via cairosvg or ImageMagick
        svg_data = raw[4:]
        log.info("Got QR SVG (%d chars), converting to PNG", len(svg_data))
        convert = subprocess.run(
            ["convert", "-background", "white", "-density", "300",
             "svg:-", "-resize", "800x800", "-threshold", "50%",
             "-depth", "8", "png:-"],
            input=svg_data.encode(),
            capture_output=True,
            timeout=10,
        )
        if convert.returncode == 0 and len(convert.stdout) > 1000:
            return Response(content=convert.stdout, media_type="image/png")
        log.warning("SVG→PNG conversion failed (rc=%d, size=%d)", convert.returncode, len(convert.stdout))
        log.warning("convert stderr: %s", convert.stderr.decode()[:200])

    raise HTTPException(
        status_code=404,
        detail=f"No QR data found. Got: {raw[:200]}",
    )


@app.get("/attachment")
async def get_attachment(path: str = Query(..., description="Relative attachment path from Signal data dir")):
    """
    Serve a Signal Desktop attachment by its relative path.

    Attachment paths come from the ``path`` field in the ``attachments`` array
    of a message's JSON column (e.g. ``attachments.noindex/abc123/image.jpg``).

    Signal Desktop 7+ (version=2) encrypts files on disk using a per-attachment
    ``localKey``.  This endpoint transparently decrypts them before serving.

    Access is restricted to files inside the Signal data directory to prevent
    directory traversal attacks.
    """
    data_dir = Path(settings.signal_data_dir).resolve()
    # Normalise the path and prevent traversal outside the data dir
    try:
        full_path = _resolve_attachment_path(settings.signal_data_dir, path)
        if full_path is None:
            raise HTTPException(status_code=404, detail=f"Attachment not found: {path}")
        full_path = full_path.resolve()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid path: {e}")

    if not str(full_path).startswith(str(data_dir)):
        raise HTTPException(status_code=403, detail="Path traversal not allowed")

    mime_type, _ = mimetypes.guess_type(str(full_path))
    content = full_path.read_bytes()

    # v2 local encryption: if the file doesn't start with a known magic
    # header, try to decrypt it using localKey from the DB.
    _PLAINTEXT_MAGIC = {
        b"\x89PNG",      # PNG
        b"\xff\xd8\xff",  # JPEG
        b"GIF8",          # GIF
        b"RIFF",          # WEBP
        b"%PDF",          # PDF
        b"PK",            # ZIP / DOCX / etc.
    }
    is_plaintext = any(content[:4].startswith(m) for m in _PLAINTEXT_MAGIC)

    if not is_plaintext and len(content) >= 64:
        local_key = get_local_key_for_path(settings.signal_data_dir, path)
        if local_key:
            try:
                content = decrypt_local_attachment(full_path, local_key)
                # Re-detect mime type from decrypted content
                if content[:4] == b"\x89PNG":
                    mime_type = "image/png"
                elif content[:3] == b"\xff\xd8\xff":
                    mime_type = "image/jpeg"
                elif content[:4] == b"GIF8":
                    mime_type = "image/gif"
                elif content[:4] == b"RIFF":
                    mime_type = "image/webp"
                log.debug("Decrypted v2 attachment: %s (%d bytes)", path, len(content))
            except Exception as e:
                log.warning("Failed to decrypt v2 attachment %s: %s", path, e)

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
    Download all pending attachments for a group directly from Signal's CDN,
    and decrypt any v2 locally-encrypted files using their ``localKey``.

    For every attachment in the group's messages:

    - **v2 on-disk encrypted** (has ``path``, ``localKey``, ``version=2``):
      Decrypted using AES-256-CBC + HMAC-SHA256 with ``localKey`` and cached
      at ``{signal_data_dir}/cdn-cache/{cdnKey}``.
    - **CDN-only** (has ``cdnKey``, ``key`` but no local ``path``):
      Downloaded from ``cdn{N}.signal.org``, decrypted, and cached.

    Files already cached are skipped.  Returns
    ``{downloaded, decrypted_local, failed, skipped}``.
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

    import json as _json

    pending_cdn: list[dict] = []
    pending_local: list[dict] = []
    skip_no_cdn = 0
    skip_cached = 0
    skip_on_disk_plain = 0
    total_att_entries = 0
    try:
        conn = _open_db(settings.signal_data_dir)
        tables = _get_all_tables(conn)

        # Signal Desktop 7+ stores attachments in a separate table
        if "message_attachments" in tables:
            ma_cols = _get_table_columns(conn, "message_attachments")
            cdn_key_col = "transitCdnKey" if "transitCdnKey" in ma_cols else "cdnKey"
            cdn_num_col = "transitCdnNumber" if "transitCdnNumber" in ma_cols else "cdnNumber"
            has_local_key = "localKey" in ma_cols
            has_version = "version" in ma_cols

            select_parts = [cdn_key_col, cdn_num_col, "key", "contentType", "path"]
            if has_local_key:
                select_parts.append("localKey")
            if has_version:
                select_parts.append("version")

            q = f"SELECT {', '.join(select_parts)} FROM message_attachments"
            params: list = []
            conditions: list[str] = []
            if "attachmentType" in ma_cols:
                conditions.append("(attachmentType IN ('standard','attachment','long-message'))")
            if conversation_id and "conversationId" in ma_cols:
                conditions.append("conversationId = ?")
                params.append(conversation_id)
            if conditions:
                q += " WHERE " + " AND ".join(conditions)

            rows = conn.execute(q, params).fetchall()
            log.info(
                "fetch-all: group=%s conv_id=%s — attachments in message_attachments table: %d",
                group_name or group_id, conversation_id or "(all groups)", len(rows),
            )
            for r in rows:
                total_att_entries += 1
                cdn_key = str(r[0] or "")
                cdn_number = r[1]
                key_b64 = str(r[2] or "")
                content_type = str(r[3] or "")
                path = str(r[4] or "")
                local_key = str(r[5] or "") if has_local_key and len(r) > 5 else ""
                version = int(r[6] or 0) if has_version and len(r) > 6 else 0

                # Already cached → skip
                if cdn_key:
                    cache_file = Path(settings.signal_data_dir) / "cdn-cache" / cdn_key
                    if cache_file.exists():
                        skip_cached += 1
                        continue

                # v2 on-disk encrypted file → decrypt locally (no CDN needed)
                if path and version == 2 and local_key:
                    resolved = _resolve_attachment_path(settings.signal_data_dir, path)
                    if resolved:
                        pending_local.append({
                            "path": str(resolved),
                            "localKey": local_key,
                            "cdnKey": cdn_key,
                            "contentType": content_type,
                        })
                        continue

                # File on disk but NOT encrypted (plaintext) → skip
                if path:
                    resolved = _resolve_attachment_path(settings.signal_data_dir, path)
                    if resolved:
                        skip_on_disk_plain += 1
                        continue

                # Need CDN download
                if not cdn_key or not key_b64:
                    skip_no_cdn += 1
                    if skip_no_cdn <= 3:
                        log.info(
                            "fetch-all: skip (no cdnKey/key): contentType=%s cdnKey=%r key_len=%d path=%r",
                            content_type, cdn_key, len(key_b64), path,
                        )
                    continue
                pending_cdn.append({
                    "cdnKey": cdn_key,
                    "cdnNumber": cdn_number,
                    "key": key_b64,
                    "contentType": content_type,
                })
        else:
            # Legacy: parse from json column
            q_broad = "SELECT COUNT(*) FROM messages WHERE json LIKE '%\"attachments\":[%' AND json NOT LIKE '%\"attachments\":[]%'"
            if conversation_id:
                q_broad += " AND conversationId = ?"
                broad_count = conn.execute(q_broad, [conversation_id]).fetchone()[0]
            else:
                broad_count = conn.execute(q_broad).fetchone()[0]
            log.info(
                "fetch-all: group=%s conv_id=%s — messages with non-empty attachments array: %d",
                group_name or group_id, conversation_id or "(all groups)", broad_count,
            )

            q = "SELECT json FROM messages WHERE json LIKE '%\"attachments\":[%' AND json NOT LIKE '%\"attachments\":[]%'"
            params = []
            if conversation_id:
                q += " AND conversationId = ?"
                params.append(conversation_id)
            rows = conn.execute(q, params).fetchall()
            log.info("fetch-all: messages with non-empty attachments (broad pattern): %d", len(rows))

            for (raw_json,) in rows:
                try:
                    msg = _json.loads(raw_json) if raw_json else {}
                    for att in msg.get("attachments") or []:
                        if not isinstance(att, dict):
                            continue
                        total_att_entries += 1
                        cdn_key = att.get("cdnKey") or ""
                        key_b64 = att.get("key") or ""
                        if cdn_key:
                            cache_file = Path(settings.signal_data_dir) / "cdn-cache" / cdn_key
                            if cache_file.exists():
                                skip_cached += 1
                                continue
                        if att.get("path"):
                            skip_on_disk_plain += 1
                            continue
                        if not cdn_key or not key_b64:
                            skip_no_cdn += 1
                            if skip_no_cdn <= 3:
                                log.info(
                                    "fetch-all: skip (no cdnKey/key): contentType=%s cdnKey=%r key_len=%d path=%r",
                                    att.get("contentType"), cdn_key, len(key_b64), att.get("path"),
                                )
                            continue
                        pending_cdn.append({
                            "cdnKey": cdn_key,
                            "cdnNumber": att.get("cdnNumber"),
                            "key": key_b64,
                            "contentType": att.get("contentType") or "",
                        })
                except Exception:
                    continue

        conn.close()
    except Exception as e:
        log.exception("fetch-all: failed to scan messages")
        return {"downloaded": 0, "failed": 0, "skipped": 0, "decrypted_local": 0, "error": str(e)}

    log.info(
        "fetch-all: total_att=%d pending_cdn=%d pending_local=%d skip_no_cdn=%d skip_cached=%d skip_on_disk_plain=%d",
        total_att_entries, len(pending_cdn), len(pending_local), skip_no_cdn, skip_cached, skip_on_disk_plain,
    )
    if not pending_cdn and not pending_local:
        log.info("fetch-all: no pending attachments for group %s", group_name or group_id)
        return {"downloaded": 0, "failed": 0, "skipped": 0, "decrypted_local": 0}

    # Load credentials once (only needed for CDN downloads)
    credentials: dict = {}
    if pending_cdn:
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
    decrypted_local = 0

    # Phase 1: Decrypt v2 on-disk encrypted files using localKey
    if pending_local:
        log.info("fetch-all: decrypting %d v2 local attachment(s) for group %s", len(pending_local), group_name or group_id)
        cache_dir = Path(settings.signal_data_dir) / "cdn-cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        for att in pending_local:
            try:
                full_path = Path(att["path"])
                plaintext = decrypt_local_attachment(full_path, att["localKey"])
                cache_key = att["cdnKey"] or Path(att["path"]).name
                cache_file = cache_dir / cache_key
                cache_file.write_bytes(plaintext)
                decrypted_local += 1
                log.debug("fetch-all: decrypted local v2 %s → %s (%d bytes)", att["path"], cache_key, len(plaintext))
            except Exception as e:
                failed += 1
                log.warning("fetch-all: failed to decrypt local v2 %s: %s", att["path"], e)

    # Phase 2: Download from CDN
    if pending_cdn:
        log.info("fetch-all: downloading %d pending CDN attachment(s) for group %s", len(pending_cdn), group_name or group_id)
        for att in pending_cdn:
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
        "fetch-all: done — downloaded=%d decrypted_local=%d failed=%d skipped=%d",
        downloaded, decrypted_local, failed, skipped,
    )
    return {"downloaded": downloaded, "decrypted_local": decrypted_local, "failed": failed, "skipped": skipped}


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
             "-resize", "800x800", "-threshold", "50%",
             "-depth", "8", "png:-"],
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
        tables = _get_all_tables(conn)

        conv_filter = ""
        params: list = []
        if conversation_id:
            conv_filter = " AND conversationId = ?"
            params = [conversation_id]

        result_data: dict = {
            "conversation_id": conversation_id,
            "has_message_attachments_table": "message_attachments" in tables,
        }

        # Method 1: Check message_attachments table (Signal Desktop 7+)
        if "message_attachments" in tables:
            ma_cols = _get_table_columns(conn, "message_attachments")
            cdn_key_col = "transitCdnKey" if "transitCdnKey" in ma_cols else "cdnKey"

            q_total = "SELECT COUNT(*) FROM message_attachments"
            ma_params: list = []
            ma_conditions: list[str] = []
            if "attachmentType" in ma_cols:
                ma_conditions.append("(attachmentType IN ('standard','attachment','long-message'))")
            if conversation_id and "conversationId" in ma_cols:
                ma_conditions.append("conversationId = ?")
                ma_params.append(conversation_id)
            if ma_conditions:
                q_total += " WHERE " + " AND ".join(ma_conditions)

            total_in_table = conn.execute(q_total, ma_params).fetchone()[0]

            has_cdn_key = 0
            has_key_b64 = 0
            has_path = 0
            has_content_type_only = 0
            samples = []

            sample_q = f"SELECT messageId, contentType, path, fileName, {cdn_key_col}, key, size FROM message_attachments"
            if ma_conditions:
                sample_q += " WHERE " + " AND ".join(ma_conditions)
            sample_q += " LIMIT 10"
            sample_rows = conn.execute(sample_q, ma_params).fetchall()

            for r in sample_rows:
                cdn_k = str(r[4] or "")
                key_b = str(r[5] or "")
                path_v = str(r[2] or "")
                ct = str(r[1] or "")
                if cdn_k:
                    has_cdn_key += 1
                if key_b:
                    has_key_b64 += 1
                if path_v:
                    has_path += 1
                if ct and not cdn_k and not path_v:
                    has_content_type_only += 1
                if len(samples) < 3:
                    samples.append({
                        "messageId": str(r[0]),
                        "contentType": ct,
                        "path": path_v[:60],
                        "fileName": str(r[3] or ""),
                        "cdnKey": cdn_k[:30] if cdn_k else "",
                        "has_key": bool(key_b),
                        "size": r[6],
                    })

            result_data["message_attachments_table"] = {
                "total_attachments": total_in_table,
                "columns": sorted(ma_cols),
                "sample_breakdown": {
                    "has_cdn_key": has_cdn_key,
                    "has_decryption_key": has_key_b64,
                    "has_path_on_disk": has_path,
                    "has_content_type_only": has_content_type_only,
                },
                "samples": samples,
            }

        # Method 2: Legacy json column check
        if "json" in _get_table_columns(conn, "messages"):
            base_q = "FROM messages WHERE json LIKE '%\"attachments\":%'"
            total_msgs = conn.execute(f"SELECT COUNT(*) {base_q}{conv_filter}", params).fetchone()[0]
            non_empty = conn.execute(
                f"SELECT COUNT(*) FROM messages WHERE json LIKE '%\"attachments\":[%' "
                f"AND json NOT LIKE '%\"attachments\":[]%'{conv_filter}",
                params
            ).fetchone()[0]

            # Dump raw attachment JSON for messages with non-empty attachments
            json_samples = []
            if non_empty > 0:
                sample_rows = conn.execute(
                    f"SELECT id, json FROM messages WHERE json LIKE '%\"attachments\":[%' "
                    f"AND json NOT LIKE '%\"attachments\":[]%'{conv_filter} LIMIT 3",
                    params
                ).fetchall()
                for r in sample_rows:
                    try:
                        msg_obj = _json.loads(r[1]) if r[1] else {}
                        att_list = msg_obj.get("attachments", [])
                        json_samples.append({
                            "msg_id": str(r[0]),
                            "attachment_count": len(att_list),
                            "attachment_keys": [
                                list(a.keys()) if isinstance(a, dict) else type(a).__name__
                                for a in att_list[:3]
                            ],
                            "first_attachment": _json.dumps(att_list[0])[:400] if att_list and isinstance(att_list[0], dict) else str(att_list[:1]),
                        })
                    except Exception:
                        json_samples.append({"msg_id": str(r[0]), "parse_error": True})

            result_data["legacy_json_column"] = {
                "messages_with_any_attachments_key": total_msgs,
                "messages_with_non_empty_attachments": non_empty,
                "samples": json_samples,
            }

        # Messages with hasAttachments flag
        msg_cols = _get_table_columns(conn, "messages")
        if "hasAttachments" in msg_cols:
            has_att_count = conn.execute(
                f"SELECT COUNT(*) FROM messages WHERE hasAttachments = 1{conv_filter}",
                params
            ).fetchone()[0]
            result_data["has_attachments_flag_count"] = has_att_count

            # For messages with hasAttachments=1, dump raw json samples
            if has_att_count > 0:
                att_msg_rows = conn.execute(
                    f"SELECT id, json, hasAttachments, hasVisualMediaAttachments FROM messages "
                    f"WHERE hasAttachments = 1{conv_filter} LIMIT 3",
                    params
                ).fetchall()
                raw_samples = []
                for r in att_msg_rows:
                    raw_json = r[1] or ""
                    try:
                        msg_obj = _json.loads(raw_json) if raw_json else {}
                        att_data = msg_obj.get("attachments", "MISSING_KEY")
                        raw_samples.append({
                            "msg_id": str(r[0]),
                            "hasAttachments": r[2],
                            "hasVisualMediaAttachments": r[3],
                            "json_has_attachments_key": "attachments" in msg_obj,
                            "attachments_value": _json.dumps(att_data)[:500] if att_data != "MISSING_KEY" else "MISSING_KEY",
                            "json_keys": list(msg_obj.keys())[:20],
                            "json_length": len(raw_json),
                        })
                    except Exception:
                        raw_samples.append({
                            "msg_id": str(r[0]),
                            "json_length": len(raw_json),
                            "json_snippet": raw_json[:300],
                        })
                result_data["messages_with_hasAttachments_samples"] = raw_samples

        # Check attachment_downloads queue
        if "attachment_downloads" in tables:
            dl_count = conn.execute("SELECT COUNT(*) FROM attachment_downloads").fetchone()[0]
            result_data["attachment_downloads_queue"] = dl_count

            # Per-group: how many downloads are for messages in this conversation?
            group_dl_count = 0
            if conversation_id:
                group_dl_count = conn.execute(
                    "SELECT COUNT(*) FROM attachment_downloads ad "
                    "INNER JOIN messages m ON ad.messageId = m.id "
                    "WHERE m.conversationId = ?",
                    (conversation_id,)
                ).fetchone()[0]
            result_data["group_attachment_downloads"] = group_dl_count

            if group_dl_count > 0:
                group_dl_samples = conn.execute(
                    "SELECT ad.messageId, ad.contentType, ad.size, ad.active, "
                    "ad.attachmentJson, ad.source "
                    "FROM attachment_downloads ad "
                    "INNER JOIN messages m ON ad.messageId = m.id "
                    "WHERE m.conversationId = ? LIMIT 3",
                    (conversation_id,)
                ).fetchall()
                result_data["group_download_samples"] = [
                    {
                        "messageId": str(r[0]),
                        "contentType": r[1],
                        "size": r[2],
                        "active": r[3],
                        "attachmentJson_preview": str(r[4] or "")[:300],
                        "source": r[5],
                    }
                    for r in group_dl_samples
                ]
            elif dl_count > 0:
                dl_sample = conn.execute(
                    "SELECT messageId, contentType, size, active, source FROM attachment_downloads LIMIT 3"
                ).fetchall()
                result_data["attachment_downloads_samples"] = [
                    {"messageId": str(r[0]), "contentType": r[1], "size": r[2], "active": r[3], "source": r[4]}
                    for r in dl_sample
                ]

        # Total messages in group
        total_msgs_in_group = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE 1=1{conv_filter}", params
        ).fetchone()[0]
        result_data["total_messages_in_group"] = total_msgs_in_group

        conn.close()
        return result_data
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
    
    await asyncio.sleep(8)
    
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
