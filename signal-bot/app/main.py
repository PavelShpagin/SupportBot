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
    insert_raw_message,
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

_bot_sender_hash = hash_sender(settings.signal_bot_e164) if settings.signal_bot_e164 else ""
deps = WorkerDeps(
    settings=settings,
    db=db,
    llm=llm,
    rag=rag,
    signal=signal,
    ultimate_agent=ultimate_agent,
    bot_sender_hash=_bot_sender_hash,
)

# Global lock for pruning to prevent concurrent runs
_prune_lock = threading.Lock()

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
    Keep DB state in sync with current Signal contacts and groups.

    When a user removes the bot from contacts/friends, we clear:
    - admin session (including language/state)
    - pending history jobs
    - admin<->group links

    When the bot is removed from a group, we unlink all admins from that group.
    
    Uses a lock to prevent concurrent runs.
    """
    # Acquire lock with timeout to prevent blocking indefinitely
    acquired = _prune_lock.acquire(blocking=False)
    if not acquired:
        log.debug("Prune already running, skipping this cycle")
        return
    
    try:
        _do_prune()
    finally:
        _prune_lock.release()


def _do_prune() -> None:
    """Internal prune logic (called with lock held)."""
    from app.db.queries_mysql import (
        list_known_admin_ids,
        list_groups_with_linked_admins,
        delete_admin_session,
        cancel_all_history_jobs_for_admin,
        unlink_admin_from_all_groups,
        unlink_all_admins_from_group,
    )

    # 1. Prune admins no longer in contacts (clear state when user removes bot)
    if hasattr(signal, "list_contacts"):
        contacts = signal.list_contacts()
        if contacts is not None:
            known_admins = list_known_admin_ids(db)
            for admin_id in known_admins:
                if admin_id in contacts:
                    continue
                log.info("Admin %s removed bot from contacts. Clearing all state.", admin_id)
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

    # 2. Prune groups where bot was removed (e.g. kicked from group)
    if isinstance(signal, NoopSignalAdapter):
        return
    try:
        current_groups = {g.group_id for g in signal.list_groups()}
    except Exception as _list_exc:
        log.warning("Failed to list groups for prune; skipping group prune: %s", _list_exc)
        return
    linked_groups = list_groups_with_linked_admins(db)
    # Safety: if signal-cli returned zero groups but the DB still has linked groups,
    # this is almost certainly a transient network error — skip the prune to avoid
    # incorrectly wiping live data.
    if not current_groups and linked_groups:
        log.warning(
            "list_groups() returned 0 groups but DB has %d linked group(s) — "
            "likely a transient Signal error; skipping group prune",
            len(linked_groups),
        )
        return
    for group_id in linked_groups:
        if group_id in current_groups:
            continue
        log.info("Bot no longer in group %s. Cleaning up all data for compliance.", group_id)
        
        # First, get case IDs for RAG cleanup before deleting from DB
        from app.db.queries_mysql import get_case_ids_for_group, delete_all_group_data
        try:
            case_ids = get_case_ids_for_group(db, group_id)
        except Exception:
            log.exception("Failed to get case IDs for group %s", group_id)
            case_ids = []
        
        # Delete from RAG (use group-level delete to catch any stale entries)
        try:
            from app.rag.chroma import create_chroma
            _prune_rag = create_chroma(settings)
            deleted_rag = _prune_rag.delete_cases_by_group(group_id)
            log.info("Deleted %d RAG docs for group %s (group-level delete)", deleted_rag, group_id)
        except Exception:
            log.exception("Failed to delete cases from RAG for group %s", group_id)
        
        # Delete all group data from DB
        try:
            stats = delete_all_group_data(db, group_id)
            log.info(
                "Deleted group data for %s: cases=%d, evidence=%d, messages=%d, reactions=%d, jobs=%d",
                group_id, stats["cases"], stats["case_evidence"], stats["raw_messages"],
                stats["reactions"], stats["jobs"]
            )
        except Exception:
            log.exception("Failed to delete group data for %s", group_id)
        
        # Unlink admins
        try:
            unlinked = unlink_all_admins_from_group(db, group_id)
            if unlinked:
                log.info("Unlinked %d admins from removed group %s", unlinked, group_id)
        except Exception:
            log.exception("Failed to unlink admins from group %s", group_id)


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

    # --- NEW: Command Handling ---
    text_lower = text.lower()
    
    # Language commands
    if text_lower in ("/en", "/english"):
        set_admin_lang(db, admin_id, "en")
        if session: session = get_admin_session(db, admin_id) # Refresh session
        msg = "Language set to English. Please enter the group name you want to link."
        if isinstance(signal, SignalCliAdapter):
            signal.send_direct_text(recipient=admin_id, text=msg)
        return

    if text_lower in ("/ua", "/uk", "/ukrainian"):
        set_admin_lang(db, admin_id, "uk")
        if session: session = get_admin_session(db, admin_id) # Refresh session
        msg = "Мову змінено на українську. Введіть назву групи для підключення."
        if isinstance(signal, SignalCliAdapter):
            signal.send_direct_text(recipient=admin_id, text=msg)
        return

    # /wipe — erase ALL bot data (groups, cases, history, sessions). Keeps phone registration.
    if text_lower == "/wipe":
        from app.db.queries_mysql import wipe_all_data
        try:
            stats = wipe_all_data(db)
            try:
                rag.wipe_all_cases()
            except Exception as e:
                log.warning("RAG wipe failed: %s", e)
            summary = ", ".join(f"{k}={v}" for k, v in stats.items())
            log.info("WIPE executed by admin %s: %s", admin_id, summary)
            msg = f"✅ Wiped all data.\n{summary}\nBot registration kept. Send group name to start fresh."
        except Exception as e:
            msg = f"❌ Wipe failed: {e}"
        signal.send_direct_text(recipient=admin_id, text=msg)
        return

    # Ignore commands other than language to prevent accidental group searches
    if text.startswith("/"):
        msg = "Unknown command. Available: /en, /uk, /wipe"
        signal.send_direct_text(recipient=admin_id, text=msg)
        return
    # -----------------------------

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
        # Brand new admin — detect language, send welcome, then fall through to group lookup
        detected_lang = _detect_language(text)
        log.info("New admin %s, detected language: %s, sending welcome", admin_id, detected_lang)
        set_admin_awaiting_group_name(db, admin_id)
        set_admin_lang(db, admin_id, detected_lang)
        if not isinstance(signal, NoopSignalAdapter):
            sent = signal.send_onboarding_prompt(recipient=admin_id, lang=detected_lang)
            if isinstance(signal, SignalCliAdapter) and not sent:
                # User blocked us - clear session
                from app.db.queries_mysql import unlink_admin_from_all_groups
                delete_admin_session(db, admin_id)
                unlink_admin_from_all_groups(db, admin_id)
                log.info("Cleared session for blocked user %s", admin_id)
                return
        # Continue to group lookup with this same message
        lang = detected_lang
    else:
        lang = session.lang

    if not text:
        # Empty message - resend prompt
        if not isinstance(signal, NoopSignalAdapter):
            signal.send_onboarding_prompt(recipient=admin_id, lang=lang)
        return
    
    # If admin sends a new message while awaiting QR scan:
    # - same group name → just say "still processing", avoid duplicate QR flow
    # - different group name → cancel current job and restart with new name
    if session is not None and session.state == "awaiting_qr_scan" and session.pending_token:
        same_group = (
            session.pending_group_name
            and text.lower().strip() == session.pending_group_name.lower().strip()
        )
        if same_group:
            log.info("Admin %s resent same group name while awaiting QR scan — ignoring duplicate", admin_id)
            wait_msg = (
                f'QR-код для групи "{session.pending_group_name}" вже генерується. Зачекайте, будь ласка.'
                if lang == "uk"
                else f'QR code for group "{session.pending_group_name}" is already being generated. Please wait.'
            )
            signal.send_direct_text(recipient=admin_id, text=wait_msg)
            return
        log.info("Admin %s sent new group name while awaiting QR scan, cancelling pending job", admin_id)
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


_POSITIVE_EMOJIS = {
    "👍", "👍🏻", "👍🏼", "👍🏽", "👍🏾", "👍🏿",
    "❤️", "❤", "🧡", "💛", "💚", "💙", "💜", "🖤", "🤍",
    "💯", "✅", "✔️", "✔", "☑️",
    "🎉", "🥳", "🏆", "⭐", "🌟",
    "👏", "🙌", "🤝", "🫡", "💪",
    "😊", "🙏", "+",
}


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

        # Close any open case linked to the reacted message
        if r.emoji in _POSITIVE_EMOJIS:
            try:
                closed_id = close_case_by_message_ts(
                    db,
                    group_id=r.group_id,
                    target_ts=r.target_ts,
                    emoji=r.emoji,
                )
                if closed_id:
                    log.info(
                        "Case %s closed via reaction %s (group=%s ts=%s)",
                        closed_id, r.emoji, r.group_id, r.target_ts,
                    )
            except Exception:
                log.exception("Failed to close case via reaction")


def _handle_contact_removed(phone_number: str) -> None:
    """
    Handle when a user removes/blocks the bot.
    
    This clears their admin session so they get a fresh start
    if they re-add the bot later.
    """
    from app.db.queries_mysql import (
        delete_admin_session,
        delete_admin_history_tokens,
        cancel_all_history_jobs_for_admin,
        unlink_admin_from_all_groups,
    )
    
    log.info("Contact removed/blocked us: %s - clearing ALL their personal data for compliance", phone_number)
    
    # Cancel any pending history jobs for this admin
    try:
        cancelled = cancel_all_history_jobs_for_admin(db, phone_number)
        if cancelled:
            log.info("Cancelled %d pending history jobs for removed contact %s", cancelled, phone_number)
    except Exception:
        log.exception("Failed to cancel history jobs for %s", phone_number)
    
    # Delete their history tokens (compliance)
    try:
        deleted_tokens = delete_admin_history_tokens(db, phone_number)
        if deleted_tokens:
            log.info("Deleted %d history tokens for removed contact %s", deleted_tokens, phone_number)
    except Exception:
        log.exception("Failed to delete history tokens for %s", phone_number)
    
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
            time.sleep(15)  # Prune every 15s so re-add gets fresh state quickly

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
            "sender_name": msg.sender_name,
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
        html = """<!DOCTYPE html><html><head><title>Case not found</title>
        <style>body{font-family:sans-serif;max-width:600px;margin:60px auto;padding:20px;color:#333}
        h2{color:#c00}.note{margin-top:16px;color:#666;font-size:14px}</style></head>
        <body><h2>Кейс не знайдено</h2>
        <p>Цей кейс було видалено або замінено під час повторної обробки історії чату.</p>
        <p class="note">Якщо ви бачите це посилання у відповіді бота — це тимчасова проблема. Новий кейс буде доступний після наступного запиту.</p>
        </body></html>"""
        return HTMLResponse(content=html, status_code=404)
    
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

    elif key == "qr_reminder":
        if lang == "uk":
            return "Ще 3 хвилини для сканування QR-коду. Не забудьте відсканувати!"
        return "3 minutes left to scan the QR code. Don't forget to scan it!"

    elif key == "syncing":
        if lang == "uk":
            return "Синхронізація Signal... зачекайте до хвилини."
        return "Syncing Signal... please wait up to a minute."

    elif key == "already_linked":
        if lang == "uk":
            return "Сесія вже активна. Збираю повідомлення без нового QR-коду..."
        return "Session already active. Collecting messages without new QR code..."

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
    
    if not isinstance(signal, NoopSignalAdapter) and progress_text:
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
                "Waiting for scan (5 min)..."
            )
        
        if not isinstance(signal, NoopSignalAdapter):
            signal.send_direct_image(recipient=admin_id, image_path=qr_path, caption=caption,
                                     retries=4, retry_delay=8.0)
        
        # Clean up temp file
        os.unlink(qr_path)
        log.info("QR code sent to %s successfully", admin_id)
        return {"ok": True}
        
    except Exception as e:
        log.exception("Failed to send QR code to user after retries")
        # Try to clean up temp file even on failure
        try:
            os.unlink(qr_path)
        except Exception:
            pass
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
    
    # Reset admin state first so the next message from admin goes through normally
    set_admin_awaiting_group_name(db, admin_id)

    if not isinstance(signal, NoopSignalAdapter):
        # Build summary text to send
        if req.success and group_id:
            link_admin_to_group(db, admin_id=admin_id, group_id=group_id)

        def _send_notifications():
            try:
                if req.success:
                    signal.send_success_message(recipient=admin_id, group_name=group_name, lang=lang)
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
            except Exception:
                log.exception("Failed to send link-result notification to admin %s", admin_id)

        threading.Thread(target=_send_notifications, daemon=True).start()

    return {"ok": True}


class HistoryMessage(BaseModel):
    message_id: str
    sender_hash: str
    sender_name: str | None = None
    ts: int
    content_text: str


class CaseBlock(BaseModel):
    case_block: str


class HistoryCasesRequest(BaseModel):
    token: str
    group_id: str
    cases: List[CaseBlock]
    messages: List[HistoryMessage] = []  # Optional: raw messages for evidence linking


def _process_history_cases_bg(req: HistoryCasesRequest) -> int:
    """Process history cases synchronously. Returns number of cases inserted."""
    import re
    from app.db import RawMessage, mark_case_in_rag
    from app.db.queries_mysql import archive_cases_for_group, clear_group_runtime_data

    # Archive existing cases (keeps old links alive with an 'archived' banner)
    # and purge them from ChromaDB so they can't be cited in new answers.
    try:
        archived = archive_cases_for_group(db, req.group_id)
        log.info("Archived %d cases for group %s (re-ingest)", archived, req.group_id[:20])
    except Exception:
        log.exception("Failed to archive cases for group %s", req.group_id[:20])
    try:
        deleted_from_rag = rag.delete_cases_by_group(req.group_id)
        log.info("Wiped %d RAG docs for group %s (group-level delete)", deleted_from_rag, req.group_id[:20])
    except Exception:
        log.exception("Failed to wipe RAG cases for group %s", req.group_id[:20])

    # Clear transient data so re-ingest starts from a clean slate.
    try:
        clear_group_runtime_data(db, req.group_id)
        log.info("Cleared messages/buffer/reactions for group %s before re-ingest", req.group_id[:20])
    except Exception:
        log.exception("Failed to clear runtime data for group %s", req.group_id[:20])

    # Store raw messages for evidence linking
    message_lookup: dict = {}
    messages_stored = 0
    for m in req.messages:
        raw_msg = RawMessage(
            message_id=m.message_id,
            group_id=req.group_id,
            ts=m.ts,
            sender_hash=m.sender_hash,
            sender_name=m.sender_name,
            content_text=m.content_text,
            image_paths=[],
            reply_to_id=None,
        )
        if insert_raw_message(db, raw_msg):
            messages_stored += 1
        # Build lookup for evidence matching
        content_key = m.content_text.strip()[:100] if m.content_text else ""
        if content_key:
            message_lookup[content_key] = m.message_id
        message_lookup[str(m.ts)] = m.message_id

    if messages_stored > 0:
        log.info("Stored %d raw messages (group=%s)", messages_stored, req.group_id[:20])

    kept = 0
    for c in req.cases:
        try:
            case = llm.make_case(case_block_text=c.case_block)
            if not case.keep:
                continue

            # Build evidence_ids: prefer LLM-extracted, fallback to content matching
            evidence_ids = list(case.evidence_ids)
            if not evidence_ids:
                for line in c.case_block.split('\n'):
                    line = line.strip()
                    if not line:
                        continue
                    if 'ts=' in line:
                        m2 = re.search(r'msg_id=(\S+)', line)
                        if m2:
                            evidence_ids.append(m2.group(1))
                        ts_m = re.search(r'ts=(\d+)', line)
                        if ts_m and ts_m.group(1) in message_lookup:
                            mid = message_lookup[ts_m.group(1)]
                            if mid not in evidence_ids:
                                evidence_ids.append(mid)
                    else:
                        ck = line[:100]
                        if ck in message_lookup:
                            mid = message_lookup[ck]
                            if mid not in evidence_ids:
                                evidence_ids.append(mid)

            log.info(
                "History case status=%s evidence_ids=%d (group=%s)",
                case.status, len(evidence_ids), req.group_id[:20],
            )

            evidence_image_paths: List[str] = []
            for mid in evidence_ids:
                msg = get_raw_message(db, message_id=mid)
                if msg:
                    evidence_image_paths.extend(p for p in msg.image_paths if p)

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
                evidence_ids=evidence_ids,
                evidence_image_paths=evidence_image_paths,
            )

            # Only index solved cases with a solution into SCRAG (B1 open cases stay out of RAG)
            if case.status == "solved" and case.solution_summary.strip():
                doc_text = "\n".join([
                    f"[SOLVED] {case.problem_title.strip()}",
                    f"Проблема: {case.problem_summary.strip()}",
                    f"Рішення: {case.solution_summary.strip()}",
                    "tags: " + ", ".join(case.tags),
                ]).strip()
                embedding = llm.embed(text=doc_text)
                metadata: dict = {"group_id": req.group_id, "status": case.status}
                if evidence_ids:
                    metadata["evidence_ids"] = evidence_ids
                if evidence_image_paths:
                    metadata["evidence_image_paths"] = evidence_image_paths
                rag.upsert_case(case_id=case_id, document=doc_text, embedding=embedding, metadata=metadata)
                mark_case_in_rag(db, case_id)
                log.info("Indexed solved history case %s in SCRAG (group=%s)", case_id, req.group_id[:20])
            else:
                log.info("Stored open/unsolved history case %s in B1 (not indexed in SCRAG, group=%s)", case_id, req.group_id[:20])

            kept += 1
        except Exception:
            log.exception("Failed to process history case block")
            continue

    log.info("History cases done: inserted=%d group=%s", kept, req.group_id[:20])
    return kept


@app.post("/history/cases")
def history_cases(req: HistoryCasesRequest) -> dict:
    if not validate_history_token(db, token=req.token, group_id=req.group_id):
        raise HTTPException(status_code=403, detail="Invalid/expired token")

    # Security: Verify bot is still in the group.
    # If signal adapter can't list groups (e.g. signal-cli not yet linked), we log a warning
    # and allow through — the ingest service already verified admin group membership via
    # Signal Desktop QR, and the one-time token provides authentication.
    try:
        current_group_ids = {g.group_id for g in signal.list_groups()}
        if req.group_id not in current_group_ids:
            log.warning("History ingest BLOCKED: bot not in group %s", req.group_id[:20])
            raise HTTPException(status_code=403, detail="Bot is not in this group")
    except HTTPException:
        raise
    except Exception as e:
        log.warning(
            "Could not verify bot group membership (signal unavailable) — allowing ingest "
            "based on token + admin-side QR verification: %s", e
        )

    # Mark token used NOW (synchronously) so concurrent retries from signal-ingest
    # cannot pass validation and trigger a second parallel ingest for the same session.
    mark_history_token_used(db, token=req.token)

    n_cases = len(req.cases)
    n_messages = len(req.messages)
    log.info("History ingest started: %d cases, %d messages (group=%s)", n_cases, n_messages, req.group_id[:20])
    inserted = _process_history_cases_bg(req)
    log.info("History ingest done: %d/%d cases inserted (group=%s)", inserted, n_cases, req.group_id[:20])
    return {"ok": True, "cases_inserted": inserted}


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
