from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.config import load_settings
from app.ingestion import ingest_message
from app.db import create_db, ensure_schema
from app.db import (
    enqueue_job,
    create_history_token as db_create_history_token,
    validate_history_token,
    mark_history_token_used,
    new_case_id,
    insert_case,
    get_raw_message,
    get_case,
    get_case_evidence,
    get_admin_session,
    set_admin_awaiting_group_name,
    set_admin_awaiting_qr_scan,
    get_admin_by_token,
    link_admin_to_group,
    upsert_reaction,
    delete_reaction,
)
from app.jobs.types import BUFFER_UPDATE, HISTORY_LINK, MAYBE_RESPOND
from app.jobs.worker import WorkerDeps, worker_loop_forever
from app.llm.client import LLMClient
from app.logging_config import configure_logging
from app.rag.chroma import create_chroma
from app.signal.adapter import NoopSignalAdapter, SignalAdapter
from app.signal.signal_cli import SignalCliAdapter, InboundGroupMessage, InboundDirectMessage, InboundReaction
from app.signal.link_device import LinkDeviceManager, is_account_registered

# Optional import for Signal Desktop adapter (not always available)
SignalDesktopAdapter = None
try:
    import importlib
    _sd_module = importlib.import_module("app.signal.signal_desktop")
    SignalDesktopAdapter = _sd_module.SignalDesktopAdapter
except (ImportError, ModuleNotFoundError):
    pass  # SignalDesktopAdapter stays None
from app.ingestion import hash_sender
from app.agent.ultimate_agent import UltimateAgent


settings = load_settings()
configure_logging(settings.log_level)
log = logging.getLogger(__name__)


db = create_db(settings)
ensure_schema(db)

rag = create_chroma(settings)
llm = LLMClient(settings)
ultimate_agent = UltimateAgent()

signal: SignalAdapter
if getattr(settings, 'use_signal_desktop', False) and SignalDesktopAdapter is not None:
    # Use Signal Desktop for both sending and receiving (no signal-cli needed)
    log.info("Using Signal Desktop adapter at %s", settings.signal_desktop_url)
    signal = SignalDesktopAdapter(settings, desktop_url=settings.signal_desktop_url)
    try:
        signal.assert_available()
        log.info("Signal Desktop adapter connected successfully")
    except Exception as e:
        log.warning("Signal Desktop not available yet: %s", e)
elif settings.signal_listener_enabled:
    signal = SignalCliAdapter(settings)
    signal.assert_available()
else:
    signal = NoopSignalAdapter()

deps = WorkerDeps(settings=settings, db=db, llm=llm, rag=rag, signal=signal, ultimate_agent=ultimate_agent)


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files (images)
# Assuming images are stored in /var/lib/signal/...
try:
    app.mount("/static", StaticFiles(directory="/var/lib/signal"), name="static")
except Exception as e:
    log.warning(f"Could not mount /var/lib/signal: {e}")

_signal_listener_started = False
_signal_listener_lock = threading.Lock()


def _maybe_start_signal_listener() -> None:
    """
    Start the signal receive loop once the account is registered/linked.

    For signal-cli: prevents infinite "User ... is not registered" spam.
    For Signal Desktop: starts polling the Signal Desktop service.
    """
    global _signal_listener_started
    if _signal_listener_started:
        return
    
    # For Signal Desktop adapter
    if SignalDesktopAdapter is not None and isinstance(signal, SignalDesktopAdapter):
        with _signal_listener_lock:
            if _signal_listener_started:
                return
            
            # Check if Signal Desktop is linked
            if not signal.is_linked():
                log.warning(
                    "Signal Desktop not linked yet. "
                    "Link it via the signal-desktop /screenshot endpoint."
                )
                return
            
            threading.Thread(
                target=signal.listen_forever,
                kwargs={
                    "on_group_message": _handle_group_message,
                    "on_direct_message": _handle_direct_message,
                    "on_reaction": _handle_reaction,
                    "on_contact_removed": _handle_contact_removed,
                },
                daemon=True,
            ).start()
            _signal_listener_started = True
            log.info("Signal Desktop listener started")
        return
    
    # For signal-cli adapter
    if not settings.signal_listener_enabled or not isinstance(signal, SignalCliAdapter):
        return

    with _signal_listener_lock:
        if _signal_listener_started:
            return
        if not is_account_registered(config_dir=settings.signal_bot_storage, e164=settings.signal_bot_e164):
            log.warning(
                "Signal account not registered/linked yet (%s). "
                "Link it via /signal/link-device/qr (debug endpoint) and then the listener will start.",
                settings.signal_bot_e164,
            )
            return

        threading.Thread(
            target=signal.listen_forever,
            kwargs={
                "on_group_message": _handle_group_message,
                "on_direct_message": _handle_direct_message,
                "on_reaction": _handle_reaction,
                "on_contact_removed": _handle_contact_removed,
            },
            daemon=True,
        ).start()
        _signal_listener_started = True
        log.info("Signal CLI listener started")


link_device = LinkDeviceManager(
    signal_cli_bin=settings.signal_cli,
    config_dir=settings.signal_bot_storage,
    expected_e164=settings.signal_bot_e164,
    device_name="SupportBot",
    link_timeout_seconds=settings.signal_link_timeout_seconds,
    on_linked=_maybe_start_signal_listener,
)


def _detect_language(text: str) -> str:
    """
    Detect language from text.
    - If Ukrainian characters detected -> uk
    - If obviously English-only -> en  
    - If unclear -> uk (default)
    """
    if not text:
        return "uk"
    
    # Ukrainian-specific characters (Cyrillic unique to Ukrainian or common)
    ukrainian_chars = set("іїєґІЇЄҐ")
    # General Cyrillic (shared with Russian, etc.)
    cyrillic_chars = set("абвгдежзийклмнопрстуфхцчшщъыьэюяАБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ")
    
    text_chars = set(text)
    
    # If any Ukrainian-specific chars -> definitely Ukrainian
    if text_chars & ukrainian_chars:
        return "uk"
    
    # If any Cyrillic chars -> assume Ukrainian
    if text_chars & cyrillic_chars:
        return "uk"
    
    # Check if text is ASCII/Latin only (likely English)
    latin_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
    text_letters = set(c for c in text if c.isalpha())
    
    if text_letters and text_letters <= latin_chars:
        # All letters are Latin - likely English
        return "en"
    
    # Default to Ukrainian
    return "uk"


def _prune_disconnected_admins() -> None:
    """
    Keep DB state in sync with current Signal contacts.

    If a user removes the bot from contacts/friends, we must clear:
    - admin session (including language/state)
    - pending history jobs
    - admin<->group links
    """
    if not isinstance(signal, SignalCliAdapter):
        return

    contacts = signal.list_contacts()
    if contacts is None:
        # Do not mutate DB when contact sync fails.
        return

    from app.db.queries_mysql import (
        list_known_admin_ids,
        delete_admin_session,
        cancel_all_history_jobs_for_admin,
        unlink_admin_from_all_groups,
    )

    known_admins = list_known_admin_ids(db)
    for admin_id in known_admins:
        if admin_id in contacts:
            continue
        log.info("Admin %s is no longer in contacts. Cleaning all state.", admin_id)
        try:
            cancel_all_history_jobs_for_admin(db, admin_id)
        except Exception:
            log.exception("Failed to cancel jobs for disconnected admin %s", admin_id)
        try:
            delete_admin_session(db, admin_id)
        except Exception:
            log.exception("Failed to delete session for disconnected admin %s", admin_id)
        try:
            unlink_admin_from_all_groups(db, admin_id)
        except Exception:
            log.exception("Failed to unlink groups for disconnected admin %s", admin_id)


def _handle_direct_message(m: InboundDirectMessage) -> None:
    """
    Handle 1:1 messages from admins.
    
    Flow:
    1. Admin sends first message (ANYTHING) -> bot sends welcome, detects lang
    2. Admin sends group name -> bot finds group, generates QR, sends it
    3. Admin scans QR -> success/fail notification, then loop back to step 2
    4. If admin sends new message mid-process -> abort current, restart with new group
    5. On contact removed -> session is deleted, so re-add goes to step 1
    
    Language commands: /uk, /en
    """
    from app.db.queries_mysql import (
        set_admin_lang,
        cancel_pending_history_jobs,
        cancel_all_history_jobs_for_admin,
        delete_admin_session,
    )

    # Reconcile stale admins first so re-added users always start fresh.
    _prune_disconnected_admins()
    
    admin_id = m.sender
    text = m.text.strip()
    
    log.info("Direct message from %s: %s", admin_id, text[:100])
    
    # Get admin session - None means brand new user
    session = get_admin_session(db, admin_id)

    # Self-heal stale onboarding sessions: if a user comes back much later,
    # restart from welcome instead of treating random text as a group name.
    if (
        session is not None
        and session.state == "awaiting_group_name"
        and session.updated_at is not None
    ):
        session_age_seconds = (datetime.utcnow() - session.updated_at).total_seconds()
        stale_after_seconds = settings.admin_session_stale_minutes * 60
        if session_age_seconds >= stale_after_seconds:
            log.info(
                "Admin %s session is stale (age=%ss >= %ss). Restarting onboarding.",
                admin_id,
                int(session_age_seconds),
                stale_after_seconds,
            )
            try:
                delete_admin_session(db, admin_id)
            except Exception:
                log.exception("Failed to delete stale session for %s", admin_id)
            session = None
    
    if session is None:
        # Brand new admin - detect language, send welcome, DON'T search yet
        detected_lang = _detect_language(text)
        log.info("New admin %s, detected language: %s, sending welcome", admin_id, detected_lang)
        set_admin_awaiting_group_name(db, admin_id)
        set_admin_lang(db, admin_id, detected_lang)
        if isinstance(signal, SignalCliAdapter):
            sent = signal.send_onboarding_prompt(recipient=admin_id, lang=detected_lang)
            if not sent:
                # User blocked us - clear session
                from app.db.queries_mysql import unlink_admin_from_all_groups
                delete_admin_session(db, admin_id)
                unlink_admin_from_all_groups(db, admin_id)
                log.info("Cleared session for blocked user %s", admin_id)
        return
    
    lang = session.lang

    if not text:
        # Empty message - resend prompt
        if isinstance(signal, SignalCliAdapter):
            signal.send_onboarding_prompt(recipient=admin_id, lang=lang)
        return
    
    # If admin sends a new message while awaiting QR scan, cancel the pending job
    # and start fresh with the new group name
    if session.state == "awaiting_qr_scan" and session.pending_token:
        log.info("Admin %s sent new message while awaiting QR scan, cancelling pending job", admin_id)
        cancel_pending_history_jobs(db, session.pending_token)
        # Reset state and continue to search for new group
        set_admin_awaiting_group_name(db, admin_id)
    
    # Try to find group by name
    if not isinstance(signal, SignalCliAdapter):
        log.warning("Signal adapter not available for group lookup")
        return
    
    # INSTANT FEEDBACK: Tell user we're searching
    signal.send_searching_message(recipient=admin_id, group_name=text, lang=lang)
    
    group = signal.find_group_by_name(text)
    
    if group is None:
        log.info("Group not found for name: %s", text)
        signal.send_group_not_found(recipient=admin_id, lang=lang)
        return
    
    log.info("Found group: %s (%s)", group.group_name, group.group_id)
    
    # INSTANT FEEDBACK: Tell user we found it and generating QR
    signal.send_processing_message(recipient=admin_id, group_name=group.group_name, lang=lang)
    
    # Cancel any existing history jobs for this admin before creating a new one
    # This ensures only ONE job runs at a time per admin (signal-cli can only link one device)
    cancelled = cancel_all_history_jobs_for_admin(db, admin_id)
    if cancelled:
        log.info("Cancelled %d existing history jobs for admin %s", cancelled, admin_id)
    
    # Generate token and create history token
    token = uuid.uuid4().hex
    db_create_history_token(
        db,
        token=token,
        admin_id=admin_id,
        group_id=group.group_id,
        ttl_minutes=settings.history_token_ttl_minutes,
    )
    
    # Update admin session
    set_admin_awaiting_qr_scan(
        db,
        admin_id=admin_id,
        group_id=group.group_id,
        group_name=group.group_name,
        token=token,
    )
    
    # Enqueue job to generate QR code (include lang for progress messages)
    enqueue_job(db, HISTORY_LINK, {
        "token": token,
        "admin_id": admin_id,
        "group_id": group.group_id,
        "lang": lang,
        "group_name": group.group_name,
    })
    
    log.info("Enqueued HISTORY_LINK job for token %s", token)


def _handle_group_message(m: InboundGroupMessage) -> None:
    """Handle messages in group chats (existing behavior)."""
    ingest_message(
        settings=settings,
        db=db,
        llm=llm,
        message_id=m.message_id,
        group_id=m.group_id,
        sender=m.sender,
        ts=m.ts,
        text=m.text,
        image_paths=m.image_paths,
        reply_to_id=m.reply_to_id,
    )


def _handle_reaction(r: InboundReaction) -> None:
    """Handle emoji reactions to messages."""
    sender_h = hash_sender(r.sender)
    
    if r.is_remove:
        delete_reaction(
            db,
            group_id=r.group_id,
            target_ts=r.target_ts,
            sender_hash=sender_h,
            emoji=r.emoji,
        )
        log.debug("Reaction removed: group=%s ts=%s emoji=%s", r.group_id, r.target_ts, r.emoji)
    else:
        upsert_reaction(
            db,
            group_id=r.group_id,
            target_ts=r.target_ts,
            target_author=r.target_author,
            sender_hash=sender_h,
            emoji=r.emoji,
        )
        log.info("Reaction added: group=%s ts=%s emoji=%s", r.group_id, r.target_ts, r.emoji)


def _handle_contact_removed(phone_number: str) -> None:
    """
    Handle when a user removes/blocks the bot.
    
    This clears their admin session so they get a fresh start
    if they re-add the bot later.
    """
    from app.db.queries_mysql import (
        delete_admin_session,
        cancel_all_history_jobs_for_admin,
        unlink_admin_from_all_groups,
    )
    
    log.info("Contact removed/blocked us: %s - clearing their session", phone_number)
    
    # Cancel any pending history jobs for this admin
    try:
        cancelled = cancel_all_history_jobs_for_admin(db, phone_number)
        if cancelled:
            log.info("Cancelled %d pending history jobs for removed contact %s", cancelled, phone_number)
    except Exception:
        log.exception("Failed to cancel history jobs for %s", phone_number)
    
    # Delete their admin session
    try:
        delete_admin_session(db, phone_number)
        log.info("Deleted admin session for removed contact: %s", phone_number)
    except Exception:
        log.exception("Failed to delete admin session for %s", phone_number)

    # Remove all admin-group links so group processing is disabled immediately.
    try:
        unlinked = unlink_admin_from_all_groups(db, phone_number)
        if unlinked:
            log.info("Removed %d group links for removed contact %s", unlinked, phone_number)
    except Exception:
        log.exception("Failed to unlink groups for %s", phone_number)


@app.on_event("startup")
def _startup() -> None:
    t = threading.Thread(target=worker_loop_forever, args=(deps,), daemon=True)
    t.start()

    def _admin_reconcile_loop() -> None:
        while True:
            try:
                _prune_disconnected_admins()
            except Exception:
                log.exception("Admin reconcile loop failed")
            time.sleep(60)

    threading.Thread(target=_admin_reconcile_loop, daemon=True).start()

    # Start listener only if the account is already linked/registered.
    _maybe_start_signal_listener()

    log.info("Startup complete")


@app.get("/signal/link-device/qr")
def signal_link_device_qr() -> Response:
    """
    Debug-only: generate and return a QR PNG for `signal-cli link`.

    Open this endpoint in a desktop browser and scan the QR on your phone:
      Signal -> Settings -> Linked devices -> Link new device
    """
    if not settings.http_debug_endpoints_enabled:
        raise HTTPException(status_code=404, detail="Not found")

    link_device.start()
    png = link_device.wait_for_qr(timeout_seconds=5.0)
    if not png:
        snap = link_device.snapshot()
        raise HTTPException(status_code=503, detail=f"QR not ready (status={snap.status})")
    return Response(content=png, media_type="image/png")


@app.get("/signal/link-device/status")
def signal_link_device_status() -> dict:
    """Debug-only: view current link-device status and last output lines."""
    if not settings.http_debug_endpoints_enabled:
        raise HTTPException(status_code=404, detail="Not found")
    snap = link_device.snapshot()
    return {
        "status": snap.status,
        "started_at": snap.started_at,
        "ended_at": snap.ended_at,
        "exit_code": snap.exit_code,
        "url": snap.url,
        "error": snap.error,
        "output_tail": snap.output_tail[-50:],
    }


@app.post("/signal/link-device/cancel")
def signal_link_device_cancel() -> dict:
    """Debug-only: cancel an in-progress link-device flow."""
    if not settings.http_debug_endpoints_enabled:
        raise HTTPException(status_code=404, detail="Not found")
    ok = link_device.cancel()
    return {"ok": ok}


@app.get("/api/cases/{case_id}")
def get_case_endpoint(case_id: str):
    case_id = case_id.strip()
    log.info(f"API Request: get_case_endpoint for {case_id}")
    
    case = get_case(db, case_id)
    if not case:
        log.warning(f"API Error: Case {case_id} not found in DB")
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")
    
    evidence = get_case_evidence(db, case_id)
    
    evidence_data = []
    for msg in evidence:
        # Transform image paths to static URLs
        # Assuming paths are like /var/lib/signal/...
        images = []
        for p in msg.image_paths:
            if p.startswith("/var/lib/signal/"):
                images.append(p.replace("/var/lib/signal/", "/static/"))
            else:
                images.append(p)

        evidence_data.append({
            "message_id": msg.message_id,
            "ts": msg.ts,
            "sender_hash": msg.sender_hash,
            "content_text": msg.content_text,
            "images": images,
            "reply_to_id": msg.reply_to_id,
        })

    case["evidence"] = evidence_data
    return case


@app.get("/")
def root() -> dict:
    return {"status": "ok", "service": "SupportBot"}


@app.get("/chat/{message_id}", response_class=HTMLResponse)
def view_chat_context(message_id: str):
    """View context for a specific chat message."""
    # We need to access the chat index. It's loaded in UltimateAgent -> ChatSearchAgent -> ChatSearchTool
    # But those are inside the worker process or ultimate agent instance.
    # For simplicity, let's load the index here on demand (or cache it).
    
    index_path = Path("data/chat_index.pkl")
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Chat index not found")
        
    try:
        # Simple caching to avoid reloading pickle on every request
        if not hasattr(view_chat_context, "tool"):
            from app.agent.chat_search_agent import ChatSearchTool
            view_chat_context.tool = ChatSearchTool(str(index_path))
            
        context = view_chat_context.tool.get_context(message_id, radius=5)
        if context == "Message not found.":
             raise HTTPException(status_code=404, detail="Message not found in index")
             
        # Format HTML
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Chat Context</title>
            <style>
                body {{ font-family: monospace; max-width: 800px; margin: 0 auto; padding: 20px; background: #f5f5f5; }}
                .message {{ padding: 5px 10px; margin-bottom: 5px; border-radius: 3px; background: white; }}
                .message.highlight {{ background: #fff3cd; border: 1px solid #ffeeba; font-weight: bold; }}
                .meta {{ color: #666; font-size: 0.8em; }}
            </style>
        </head>
        <body>
            <h3>Chat Context</h3>
            <div class="chat-log">
        """
        
        # Parse the plain text context returned by tool to make it nicer
        # Context format: ">>> [HH:MM] Sender: Text"
        for line in context.splitlines():
            is_target = line.startswith(">>>")
            clean_line = line.replace(">>> ", "").replace("    ", "")
            css_class = "message highlight" if is_target else "message"
            html += f'<div class="{css_class}">{clean_line}</div>'
            
        html += """
            </div>
        </body>
        </html>
        """
        return HTMLResponse(content=html)
        
    except Exception as e:
        log.exception("Error viewing chat context")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/case/{case_id}", response_class=HTMLResponse)
def view_case(case_id: str):
    case = get_case(db, case_id)
    
    # Fallback: Check static JSON if not in DB
    if not case:
        try:
            import json
            cases_path = Path("data/signal_cases_structured.json")
            if cases_path.exists():
                with open(cases_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # Check if case_id matches "idx" (integer)
                    if case_id.isdigit():
                        idx = int(case_id)
                        for c in data.get("cases", []):
                            if c.get("idx") == idx:
                                # Adapt to expected format
                                case = {
                                    "problem_title": "Case #" + str(idx),
                                    "status": "solved",
                                    "problem_summary": c.get("problem_summary"),
                                    "solution_summary": c.get("solution_summary"),
                                }
                                # Static cases don't have detailed message evidence in the JSON usually, 
                                # or it's in a different format. We'll just show the summary.
                                break
        except Exception as e:
            log.warning(f"Failed to lookup static case: {e}")

    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    
    evidence = get_case_evidence(db, case_id)
    
    # Simple HTML template
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Case {case_id}</title>
        <style>
            body {{ font-family: sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }}
            .case-header {{ border-bottom: 1px solid #ccc; padding-bottom: 10px; margin-bottom: 20px; }}
            .status {{ display: inline-block; padding: 5px 10px; border-radius: 5px; background: #eee; }}
            .status.solved {{ background: #d4edda; color: #155724; }}
            .message {{ border: 1px solid #eee; padding: 10px; margin-bottom: 10px; border-radius: 5px; }}
            .meta {{ color: #666; font-size: 0.9em; margin-bottom: 5px; }}
            img {{ max-width: 100%; height: auto; margin-top: 10px; }}
        </style>
    </head>
    <body>
        <div class="case-header">
            <h1>{case.get('problem_title', 'Case ' + case_id)}</h1>
            <div class="status {case.get('status', 'open')}">{case.get('status', 'open')}</div>
            <p><strong>Problem:</strong> {case.get('problem_summary', '')}</p>
            <p><strong>Solution:</strong> {case.get('solution_summary', '')}</p>
        </div>
        
        <h2>Evidence</h2>
        <div class="evidence-list">
    """
    
    if evidence:
        for msg in evidence:
            ts_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(msg.ts / 1000))
            html += f"""
                <div class="message">
                    <div class="meta">{msg.sender_hash[:8]} at {ts_str}</div>
                    <div class="content">{msg.content_text}</div>
            """
            for p in msg.image_paths:
                # Serve images via /static if possible, or just show path
                if p.startswith("/var/lib/signal/"):
                    url = p.replace("/var/lib/signal/", "/static/")
                    html += f'<img src="{url}" loading="lazy" />'
            html += "</div>"
    else:
        html += "<p><em>No detailed message evidence available for this case.</em></p>"
        
    html += """
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


class HistoryTokenRequest(BaseModel):
    admin_id: str = Field(..., description="Signal admin identifier (string)")
    group_id: str = Field(..., description="Signal group identifier (string)")


class HistoryTokenResponse(BaseModel):
    token: str


@app.post("/history/token", response_model=HistoryTokenResponse)
def create_history_token_endpoint(req: HistoryTokenRequest) -> HistoryTokenResponse:
    """Manual token creation (for API usage, bypasses admin flow)."""
    if not settings.http_debug_endpoints_enabled:
        raise HTTPException(status_code=404, detail="Not found")
    token = uuid.uuid4().hex
    db_create_history_token(db, token=token, admin_id=req.admin_id, group_id=req.group_id, ttl_minutes=settings.history_token_ttl_minutes)
    enqueue_job(db, HISTORY_LINK, {"token": token, "admin_id": req.admin_id, "group_id": req.group_id})
    return HistoryTokenResponse(token=token)


@app.get("/history/qr/{token}")
def history_qr(token: str) -> FileResponse:
    p = Path("/var/lib/history") / f"{token}.png"
    if not p.exists():
        raise HTTPException(status_code=404, detail="QR not ready (signal-ingest has not produced it yet)")
    return FileResponse(path=str(p), media_type="image/png")


class QRReadyRequest(BaseModel):
    """Called by signal-ingest when QR is generated."""
    token: str
    admin_id: str
    group_name: str
    qr_path: str


@app.post("/history/qr-ready")
def history_qr_ready(req: QRReadyRequest) -> dict:
    """
    Callback from signal-ingest when QR code is ready.
    Sends the QR image to the admin via Signal.
    """
    log.info("QR ready for admin %s, group %s", req.admin_id, req.group_name)
    
    session = get_admin_by_token(db, req.token)
    if session is None:
        log.warning("QR ready: no admin session found for token %s", req.token)
        return {"ok": False, "error": "session_not_found"}
    if session.admin_id != req.admin_id:
        log.warning("QR ready: admin_id mismatch for token %s", req.token)
        return {"ok": False, "error": "admin_mismatch"}
    
    if isinstance(signal, SignalCliAdapter):
        # Do NOT trust inbound file paths. Always use the expected history directory path.
        qr_path = str(Path("/var/lib/history") / f"{req.token}.png")
        signal.send_qr_for_group(
            recipient=req.admin_id,
            group_name=req.group_name,
            qr_path=qr_path,
            lang=session.lang,
        )
    
    return {"ok": True}


class HistoryScanReceivedRequest(BaseModel):
    """Called by signal-ingest when QR is scanned and history processing starts."""
    token: str


@app.post("/history/scan-received")
def history_scan_received(req: HistoryScanReceivedRequest) -> dict:
    """
    Callback from signal-ingest when QR code is scanned.
    Notifies the admin that processing has started.
    """
    session = get_admin_by_token(db, req.token)
    if session is None:
        log.warning("No admin session found for token %s (scan-received)", req.token)
        return {"ok": False, "error": "session_not_found"}
    
    admin_id = session.admin_id
    group_name = session.pending_group_name or "Unknown"
    
    if isinstance(signal, SignalCliAdapter):
        signal.send_scan_received_message(recipient=admin_id, group_name=group_name, lang=session.lang)
    
    return {"ok": True}


class HistoryProgressRequest(BaseModel):
    """Called by signal-ingest to send progress updates."""
    token: str
    progress_key: str  # 'collecting', 'found_messages', 'processing_chunk', 'saving_cases'
    count: int | None = None
    current: int | None = None
    total: int | None = None


def _estimate_processing_time(message_count: int) -> tuple[int, int]:
    """Estimate processing time in seconds based on message count.
    
    Returns (min_seconds, max_seconds) tuple.
    Rough estimate: ~5-10 seconds per 100 messages for LLM processing.
    """
    base_time = 30  # Base overhead (startup, API calls, etc.)
    per_100_msgs_min = 5
    per_100_msgs_max = 15
    
    chunks = max(1, message_count // 100)
    min_time = base_time + (chunks * per_100_msgs_min)
    max_time = base_time + (chunks * per_100_msgs_max)
    
    return (min_time, max_time)


def _format_time_estimate(min_sec: int, max_sec: int, lang: str) -> str:
    """Format time estimate in human-readable form."""
    def fmt(sec: int) -> str:
        if sec < 60:
            return f"{sec} {'сек' if lang == 'uk' else 'sec'}"
        elif sec < 120:
            return f"1 {'хв' if lang == 'uk' else 'min'}"
        else:
            mins = sec // 60
            return f"{mins} {'хв' if lang == 'uk' else 'min'}"
    
    if max_sec < 60:
        return f"~{fmt(max_sec)}"
    elif min_sec // 60 == max_sec // 60:
        return f"~{fmt(max_sec)}"
    else:
        return f"{fmt(min_sec)}-{fmt(max_sec)}"


def _format_progress_message(key: str, lang: str, **kwargs) -> str:
    """Format progress message based on key and language."""
    count = kwargs.get("count", 0)
    current = kwargs.get("current", 0)
    total = kwargs.get("total", 0)
    
    if key == "collecting":
        if count and int(count) > 0:
            if lang == "uk":
                return f"Збираю повідомлення з історії чату... (вже {count})"
            else:
                return f"Collecting messages from chat history... ({count} so far)"
        return "Збираю повідомлення з історії чату..." if lang == "uk" else "Collecting messages from chat history..."
    
    elif key == "found_messages":
        min_sec, max_sec = _estimate_processing_time(count)
        time_est = _format_time_estimate(min_sec, max_sec, lang)
        if lang == "uk":
            return f"Знайдено {count} повідомлень. Аналізую...\nОрієнтовний час: {time_est}. Повідомлю, коли буде готово."
        else:
            return f"Found {count} messages. Analyzing...\nEstimated time: {time_est}. I'll notify you when ready."
    
    elif key == "processing_chunk":
        if lang == "uk":
            return f"Обробляю частину {current}/{total}..."
        else:
            return f"Processing chunk {current}/{total}..."
    
    elif key == "saving_cases":
        if lang == "uk":
            return f"Знайдено {count} вирішених кейсів. Зберігаю в базу знань..."
        else:
            return f"Found {count} solved cases. Saving to knowledge base..."
    
    elif key == "qr_sent":
        # QR code was sent separately with instructions, no additional message needed
        return ""
    
    return key


@app.post("/history/progress")
def history_progress(req: HistoryProgressRequest) -> dict:
    """
    Callback from signal-ingest to send progress updates during history processing.
    """
    session = get_admin_by_token(db, req.token)
    if session is None:
        log.warning("No admin session found for token %s (progress)", req.token)
        return {"ok": False, "error": "session_not_found"}
    
    admin_id = session.admin_id
    lang = session.lang
    
    # Format message based on progress key
    progress_text = _format_progress_message(
        req.progress_key, 
        lang, 
        count=req.count or 0, 
        current=req.current or 0, 
        total=req.total or 0
    )
    
    if isinstance(signal, SignalCliAdapter) and progress_text:
        signal.send_direct_text(recipient=admin_id, text=progress_text)
    
    return {"ok": True}


class HistoryQrCodeRequest(BaseModel):
    """Called by signal-ingest to send QR code image to user."""
    token: str
    qr_image_base64: str


@app.post("/history/qr-code")
def history_qr_code(req: HistoryQrCodeRequest) -> dict:
    """
    Callback from signal-ingest to send QR code to user for scanning.
    """
    import base64
    import tempfile
    import os
    
    session = get_admin_by_token(db, req.token)
    if session is None:
        log.warning("No admin session found for token %s (qr-code)", req.token)
        return {"ok": False, "error": "session_not_found"}
    
    admin_id = session.admin_id
    lang = session.lang
    
    # Decode and save QR image temporarily
    try:
        qr_bytes = base64.b64decode(req.qr_image_base64)
        
        # Save to temp file
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(qr_bytes)
            qr_path = f.name
        
        # Send QR code to user with instructions
        if lang == "uk":
            caption = (
                "Відскануйте цей QR-код у Signal:\n\n"
                "1. Відкрийте Signal на телефоні\n"
                "2. Налаштування → Пов'язані пристрої → Додати пристрій\n"
                "3. Відскануйте QR-код\n\n"
                "Очікую сканування (2 хв)..."
            )
        else:
            caption = (
                "Scan this QR code in Signal:\n\n"
                "1. Open Signal on your phone\n"
                "2. Settings → Linked Devices → Link New Device\n"
                "3. Scan the QR code\n\n"
                "Waiting for scan (2 min)..."
            )
        
        if isinstance(signal, SignalCliAdapter):
            # Send QR image with instructions as caption (single message)
            signal.send_direct_image(recipient=admin_id, image_path=qr_path, caption=caption)
        
        # Clean up temp file
        os.unlink(qr_path)
        
        return {"ok": True}
        
    except Exception as e:
        log.exception("Failed to send QR code to user")
        return {"ok": False, "error": str(e)}


class HistoryLinkResultRequest(BaseModel):
    """Called by signal-ingest when processing completes or fails."""
    token: str
    success: bool
    message_count: int | None = None
    cases_found: int | None = None
    cases_inserted: int | None = None
    note: str | None = None


@app.post("/history/link-result")
def history_link_result(req: HistoryLinkResultRequest) -> dict:
    """
    Callback from signal-ingest when history processing completes.
    Notifies the admin of success/failure and resets their state.
    """
    session = get_admin_by_token(db, req.token)
    if session is None:
        log.warning("No admin session found for token %s", req.token)
        return {"ok": False, "error": "session_not_found"}
    
    admin_id = session.admin_id
    group_name = session.pending_group_name or "Unknown"
    group_id = session.pending_group_id
    lang = session.lang
    
    if isinstance(signal, SignalCliAdapter):
        if req.success:
            signal.send_success_message(recipient=admin_id, group_name=group_name, lang=lang)
            if group_id:
                link_admin_to_group(db, admin_id=admin_id, group_id=group_id)
        else:
            signal.send_failure_message(recipient=admin_id, group_name=group_name, lang=lang)

        # Optional: send a short summary of what was actually imported.
        if req.message_count is not None or req.cases_inserted is not None or req.note:
            if lang == "uk":
                summary = (
                    f"Підсумок імпорту: повідомлень={req.message_count if req.message_count is not None else '?'}"
                    f", кейсів додано={req.cases_inserted if req.cases_inserted is not None else '?'}."
                )
                if req.note:
                    summary += f"\n{req.note}"
            else:
                summary = (
                    f"Import summary: messages={req.message_count if req.message_count is not None else '?'}"
                    f", cases_added={req.cases_inserted if req.cases_inserted is not None else '?'}."
                )
                if req.note:
                    summary += f"\n{req.note}"
            signal.send_direct_text(recipient=admin_id, text=summary)
    
    # Reset admin state to allow connecting another group
    set_admin_awaiting_group_name(db, admin_id)
    
    return {"ok": True}


class CaseBlock(BaseModel):
    case_block: str


class HistoryCasesRequest(BaseModel):
    token: str
    group_id: str
    cases: List[CaseBlock]


@app.post("/history/cases")
def history_cases(req: HistoryCasesRequest) -> dict:
    if not validate_history_token(db, token=req.token, group_id=req.group_id):
        raise HTTPException(status_code=403, detail="Invalid/expired token")

    kept = 0
    for c in req.cases:
        case = llm.make_case(case_block_text=c.case_block)
        if not case.keep:
            continue
        if case.status == "solved" and not case.solution_summary.strip():
            log.warning("Rejecting solved case without solution_summary (history)")
            continue

        evidence_image_paths: List[str] = []
        for mid in case.evidence_ids:
            msg = get_raw_message(db, message_id=mid)
            if msg is None:
                continue
            for p in msg.image_paths:
                if p:
                    evidence_image_paths.append(p)

        case_id = new_case_id(db)
        insert_case(
            db,
            case_id=case_id,
            group_id=req.group_id,
            status=case.status,
            problem_title=case.problem_title,
            problem_summary=case.problem_summary,
            solution_summary=case.solution_summary,
            tags=case.tags,
            evidence_ids=case.evidence_ids,
            evidence_image_paths=evidence_image_paths,
        )

        doc_text = "\n".join(
            [
                case.problem_title.strip(),
                case.problem_summary.strip(),
                case.solution_summary.strip(),
                "tags: " + ", ".join(case.tags),
            ]
        ).strip()
        embedding = llm.embed(text=doc_text)
        metadata = {
            "group_id": req.group_id,
            "status": case.status,
            "evidence_ids": case.evidence_ids,
        }
        if evidence_image_paths:
            metadata["evidence_image_paths"] = evidence_image_paths

        rag.upsert_case(
            case_id=case_id,
            document=doc_text,
            embedding=embedding,
            metadata=metadata,
        )
        kept += 1

    mark_history_token_used(db, token=req.token)
    return {"ok": True, "cases_inserted": kept}


class RetrieveRequest(BaseModel):
    group_id: str
    query: str
    k: int = Field(default=5, ge=1, le=20)


@app.post("/retrieve")
def retrieve(req: RetrieveRequest) -> dict:
    if not settings.http_debug_endpoints_enabled:
        raise HTTPException(status_code=404, detail="Not found")
    emb = llm.embed(text=req.query)
    res = rag.retrieve_cases(group_id=req.group_id, embedding=emb, k=req.k)
    return {"cases": res}


class DebugIngestRequest(BaseModel):
    group_id: str
    sender: str
    text: str = ""
    reply_to_id: Optional[str] = None


@app.post("/debug/ingest")
def debug_ingest(req: DebugIngestRequest) -> dict:
    if not settings.http_debug_endpoints_enabled:
        raise HTTPException(status_code=404, detail="Not found")
    msg_id = uuid.uuid4().hex
    ts = int(time.time() * 1000)

    ingest_message(
        settings=settings,
        db=db,
        llm=llm,
        message_id=msg_id,
        group_id=req.group_id,
        sender=req.sender,
        ts=ts,
        text=req.text,
        image_paths=[],
        reply_to_id=req.reply_to_id,
    )
    return {"ok": True, "message_id": msg_id}
