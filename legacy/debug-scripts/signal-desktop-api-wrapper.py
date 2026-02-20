"""
Signal Desktop API wrapper for ARM64 Flatpak installation.
Provides HTTP endpoints for the ingest service to interact with Signal Desktop.
"""
import asyncio
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Signal Desktop API")

# Config
SIGNAL_DATA_DIR = Path(os.getenv("SIGNAL_DATA_DIR", "/home/opc/.var/app/org.signal.Signal/config/Signal"))
DISPLAY = os.getenv("DISPLAY", ":99")


def get_db_path() -> Path:
    return SIGNAL_DATA_DIR / "sql" / "db.sqlite"


def get_key() -> Optional[str]:
    config_path = SIGNAL_DATA_DIR / "config.json"
    if not config_path.exists():
        return None
    import json
    try:
        with open(config_path) as f:
            data = json.load(f)
            return data.get("key")
    except Exception:
        return None


def open_db():
    """Open Signal Desktop's SQLCipher database."""
    db_path = get_db_path()
    key = get_key()
    if not db_path.exists() or not key:
        return None
    
    try:
        import pysqlcipher3.dbapi2 as sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.execute(f"PRAGMA key = \"x'{key}'\";")
        # Test connection
        conn.execute("SELECT count(*) FROM sqlite_master;")
        return conn
    except Exception as e:
        log.warning("Failed to open DB: %s", e)
        return None


def is_db_available() -> bool:
    conn = open_db()
    if conn:
        conn.close()
        return True
    return False


def get_conversations() -> List[Dict[str, Any]]:
    conn = open_db()
    if not conn:
        return []
    
    import json
    cursor = conn.execute("SELECT json FROM conversations")
    results = []
    for row in cursor:
        try:
            results.append(json.loads(row[0]))
        except Exception:
            pass
    conn.close()
    return results


def get_group_messages(group_id: str, group_name: Optional[str] = None, limit: int = 800) -> List[Dict[str, Any]]:
    conn = open_db()
    if not conn:
        return []
    
    import json
    
    # First find the conversation ID for this group
    conv_id = None
    cursor = conn.execute("SELECT id, json FROM conversations")
    for row in cursor:
        try:
            data = json.loads(row[1])
            if data.get("groupId") == group_id or data.get("id") == group_id:
                conv_id = row[0]
                break
            if group_name and data.get("name") == group_name:
                conv_id = row[0]
                break
        except Exception:
            pass
    
    if not conv_id:
        log.warning("Group not found: %s / %s", group_id, group_name)
        conn.close()
        return []
    
    # Get messages for this conversation
    cursor = conn.execute(
        "SELECT json FROM messages WHERE conversationId = ? ORDER BY sent_at DESC LIMIT ?",
        (conv_id, limit)
    )
    messages = []
    for row in cursor:
        try:
            msg = json.loads(row[0])
            messages.append({
                "id": msg.get("id"),
                "conversation_id": conv_id,
                "timestamp": msg.get("sent_at") or msg.get("received_at") or 0,
                "sender": msg.get("source") or msg.get("sourceUuid") or "unknown",
                "body": msg.get("body") or "",
                "type": msg.get("type"),
                "group_id": group_id,
                "group_name": group_name,
            })
        except Exception:
            pass
    conn.close()
    return messages


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/status")
async def status():
    db_available = is_db_available()
    conversations_count = 0
    has_user_conversations = False
    
    if db_available:
        try:
            convs = get_conversations()
            conversations_count = len(convs)
            for c in convs:
                if c.get("type") == "group":
                    has_user_conversations = True
                    break
                profile = c.get("profileName") or ""
                if c.get("type") == "private" and profile and profile != "Signal":
                    has_user_conversations = True
                    break
        except Exception as e:
            log.warning("Failed to get conversations: %s", e)
    
    is_linked = db_available and has_user_conversations
    
    return {
        "linked": is_linked,
        "db_available": db_available,
        "conversations_count": conversations_count,
        "has_user_conversations": has_user_conversations,
        "signal_data_dir": str(SIGNAL_DATA_DIR),
    }


@app.get("/conversations")
async def list_conversations():
    if not is_db_available():
        raise HTTPException(status_code=503, detail="Signal Desktop not ready")
    try:
        convs = get_conversations()
        return {"conversations": convs}
    except Exception as e:
        log.exception("Failed to get conversations")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/group/{group_id}/messages")
async def get_group_history(
    group_id: str,
    group_name: Optional[str] = Query(None),
    limit: int = Query(800),
):
    if not is_db_available():
        raise HTTPException(status_code=503, detail="Signal Desktop not ready")
    try:
        msgs = get_group_messages(group_id, group_name, limit)
        return {
            "messages": msgs,
            "count": len(msgs),
            "group_id": group_id,
            "group_name": group_name,
        }
    except Exception as e:
        log.exception("Failed to get group messages")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/screenshot")
async def take_screenshot(crop_qr: bool = Query(True)):
    try:
        import pyscreenshot as ImageGrab
        os.environ["DISPLAY"] = DISPLAY
        im = ImageGrab.grab()
        
        import io
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return Response(content=buf.getvalue(), media_type="image/png")
    except Exception as e:
        log.exception("Screenshot failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/reset")
async def reset_signal_desktop():
    log.info("Resetting Signal Desktop - clearing data directory: %s", SIGNAL_DATA_DIR)
    
    # Kill Signal Desktop process
    try:
        subprocess.run(["pkill", "-f", "signal-desktop"], timeout=5)
        log.info("Killed Signal Desktop process")
    except Exception as e:
        log.warning("Failed to kill Signal Desktop: %s", e)
    
    await asyncio.sleep(2)
    
    # Clear data directory (keep the directory itself)
    try:
        if SIGNAL_DATA_DIR.exists():
            for item in SIGNAL_DATA_DIR.iterdir():
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            log.info("Cleared Signal data directory")
    except Exception as e:
        log.exception("Failed to clear Signal data directory")
        raise HTTPException(status_code=500, detail=f"Failed to clear data: {e}")
    
    # Restart Signal Desktop
    try:
        subprocess.Popen(
            ["flatpak", "run", "org.signal.Signal", "--no-sandbox"],
            env={**os.environ, "DISPLAY": DISPLAY},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info("Started Signal Desktop")
    except Exception as e:
        log.exception("Failed to start Signal Desktop")
        raise HTTPException(status_code=500, detail=f"Failed to start Signal Desktop: {e}")
    
    await asyncio.sleep(5)
    
    return {"status": "reset", "message": "Signal Desktop reset. Use /screenshot to get QR code."}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
