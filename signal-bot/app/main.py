from __future__ import annotations

import logging
import mimetypes
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
    upsert_case,
    confirm_cases_by_evidence_ts,
    store_case_embedding,
    find_similar_case,
    merge_case,
    POSITIVE_EMOJI,
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
from app.jobs.types import BUFFER_UPDATE, HISTORY_LINK, MAYBE_RESPOND, SYNC_GROUP_DOCS
from app.jobs.worker import WorkerDeps, worker_loop_forever, get_worker_heartbeat_age
from app.llm.client import LLMClient
from app.logging_config import configure_logging
from app.rag.chroma import create_chroma
from app.signal.adapter import NoopSignalAdapter, SignalAdapter
from app import r2 as _r2
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

# Expand admin whitelist: resolve phone numbers → UUIDs so both formats match
if settings.admin_whitelist and isinstance(signal, SignalCliAdapter):
    phones = [p for p in settings.admin_whitelist if p.startswith("+")]
    if phones:
        phone_to_uuid = signal.resolve_phone_to_uuid(phones)
        for phone, resolved_uuid in phone_to_uuid.items():
            if resolved_uuid not in settings.admin_whitelist:
                settings.admin_whitelist.append(resolved_uuid)
                log.info("Whitelist expanded: %s → %s", phone, resolved_uuid)

# Expand superadmin list similarly
if settings.superadmin_list and isinstance(signal, SignalCliAdapter):
    phones = [p for p in settings.superadmin_list if p.startswith("+")]
    if phones:
        phone_to_uuid = signal.resolve_phone_to_uuid(phones)
        for phone, resolved_uuid in phone_to_uuid.items():
            if resolved_uuid not in settings.superadmin_list:
                settings.superadmin_list.append(resolved_uuid)
                log.info("Superadmin expanded: %s → %s", phone, resolved_uuid)
    # Superadmins must also be in the admin whitelist to DM the bot
    for sa in list(settings.superadmin_list):
        if sa not in settings.admin_whitelist:
            settings.admin_whitelist.append(sa)

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

from app.jobs.group_debouncer import GroupDebouncer
_debouncer = GroupDebouncer(deps=deps)

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


@app.get("/r2/{path:path}")
def r2_proxy(path: str) -> Response:
    """Proxy Cloudflare R2 object by key.

    Serves R2-stored attachments without requiring the bucket to be public.
    The bot fetches the object server-side using R2 credentials and streams
    the bytes to the client with a long-lived cache header.
    """
    result = _r2.download(path)
    if result is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    data, content_type = result
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )

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
                    "on_group_update": _handle_group_update,
                    "on_remote_delete": _handle_remote_delete,
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
                "on_remote_delete": _handle_remote_delete,
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

        # Delete R2 media (all attachments under attachments/{group_id}/)
        try:
            from app import r2
            if r2.is_enabled():
                deleted_r2 = r2.delete_prefix(f"attachments/{group_id}/")
                log.info("Deleted %d R2 objects for group %s", deleted_r2, group_id)
        except Exception:
            log.exception("Failed to delete R2 objects for group %s", group_id)

        # Delete all group data from DB
        try:
            stats = delete_all_group_data(db, group_id)
            log.info(
                "Deleted group data for %s: cases=%d, evidence=%d, messages=%d, reactions=%d, jobs=%d, buffer=%d",
                group_id, stats["cases"], stats["case_evidence"], stats["raw_messages"],
                stats["reactions"], stats["jobs"], stats["buffer"]
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
    
    Language commands: /ua, /en
    """
    from app.db.queries_mysql import (
        set_admin_lang,
        cancel_all_history_jobs_for_admin,
        delete_admin_session,
    )

    # Reconcile stale admins first so re-added users always start fresh.
    _prune_disconnected_admins()
    
    admin_id = m.sender
    # Strip Unicode directional markers (Signal wraps group names in U+2068/U+2069)
    text = m.text.strip().replace('\u2068', '').replace('\u2069', '').replace('\u200e', '').replace('\u200f', '').replace('\u202a', '').replace('\u202c', '')

    log.info("Direct message from %s: %s", admin_id, text[:100])

    # Whitelist check: only allowed phone numbers can interact
    if settings.admin_whitelist and admin_id not in settings.admin_whitelist:
        log.warning("Rejected DM from non-whitelisted user: %s", admin_id)
        return

    # Get admin session - None means brand new user
    session = get_admin_session(db, admin_id)

    # --- NEW: Command Handling ---
    text_lower = text.lower()
    
    # Language commands
    if text_lower in ("/en", "/english"):
        set_admin_lang(db, admin_id, "en")
        if session: session = get_admin_session(db, admin_id) # Refresh session
        msg = "Language set to English. Please enter the group name you want to link."
        if not isinstance(signal, NoopSignalAdapter):
            _send_direct_or_cleanup(admin_id, msg)
        return

    if text_lower in ("/ua", "/uk", "/ukrainian"):
        set_admin_lang(db, admin_id, "uk")
        if session: session = get_admin_session(db, admin_id) # Refresh session
        msg = "Мову змінено на українську. Введіть назву групи для підключення."
        if not isinstance(signal, NoopSignalAdapter):
            _send_direct_or_cleanup(admin_id, msg)
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
        _send_direct_or_cleanup(admin_id, msg)
        return

    # /union group1, group2, ... — unify RAG and docs across groups
    if text_lower.startswith("/union"):
        from app.db.queries_mysql import (
            get_admin_group_ids, set_union, get_groups_in_union,
            get_union_group_ids,
        )
        import uuid as _uuid
        args = text[len("/union"):].strip()
        if not args:
            _send_direct_or_cleanup(admin_id, "Usage: /union Group Name 1, Group Name 2, ...")
            return
        group_names = [n.strip() for n in args.split(",") if n.strip()]
        if len(group_names) < 2:
            _send_direct_or_cleanup(admin_id, "Need at least 2 group names separated by commas.")
            return
        admin_group_ids = set(get_admin_group_ids(db, admin_id))
        resolved = []
        for gname in group_names:
            g = signal.find_group_by_name(gname)
            if not g:
                _send_direct_or_cleanup(admin_id, f"Group not found: \"{gname}\"")
                return
            if g.group_id not in admin_group_ids:
                _send_direct_or_cleanup(admin_id, f"You haven't linked group \"{gname}\" yet.")
                return
            # Check if already in a different union
            existing = get_union_group_ids(db, g.group_id)
            if len(existing) > 1:
                _send_direct_or_cleanup(
                    admin_id,
                    f"Group \"{gname}\" is already in a union. Use /split first.",
                )
                return
            resolved.append((gname, g.group_id))
        union_id = _uuid.uuid4().hex[:16]
        set_union(db, [gid for _, gid in resolved], union_id)
        names_str = ", ".join(f"\"{n}\"" for n, _ in resolved)
        _send_direct_or_cleanup(admin_id, f"Union created: {names_str}\nThese groups now share cases and docs.")
        return

    # /split — reset all unions for the admin's groups
    if text_lower == "/split":
        from app.db.queries_mysql import get_admin_group_ids, clear_union, get_union_group_ids
        admin_group_ids = get_admin_group_ids(db, admin_id)
        all_union_gids = set()
        for gid in admin_group_ids:
            union_gids = get_union_group_ids(db, gid)
            if len(union_gids) > 1:
                all_union_gids.update(union_gids)
        if not all_union_gids:
            _send_direct_or_cleanup(admin_id, "No unions to split.")
            return
        clear_union(db, list(all_union_gids))
        _send_direct_or_cleanup(admin_id, f"Split complete. {len(all_union_gids)} groups are now independent.")
        return

    # /tag GroupName, +380..., +380... — set per-group mention targets for escalation
    if text_lower.startswith("/tag"):
        import re as _re
        from app.db.queries_mysql import set_tag_targets, get_admin_group_ids
        _tag_lang = getattr(session, "lang", "uk") or "uk"
        _tag_usage = ("Використання: /tag Назва Групи, +380..., +380..." if _tag_lang == "uk"
                      else "Usage: /tag Group Name, +380..., +380...")
        args = text[len("/tag"):].strip()
        if not args:
            _send_direct_or_cleanup(admin_id, _tag_usage)
            return
        # Comma-separated: first item = group name, rest = phone numbers
        parts = [p.strip() for p in args.split(",") if p.strip()]
        if len(parts) < 2:
            _send_direct_or_cleanup(admin_id, _tag_usage)
            return
        group_name = parts[0]
        phone_re = _re.compile(r"^\+\d{7,15}$")
        phones = []
        for p in parts[1:]:
            if not phone_re.match(p):
                _send_direct_or_cleanup(admin_id, f"{'Невірний номер' if _tag_lang == 'uk' else 'Invalid phone number'}: {p}\n{'Формат' if _tag_lang == 'uk' else 'Format'}: +380XXXXXXXXX")
                return
            phones.append(p)
        if not phones:
            _send_direct_or_cleanup(admin_id, _tag_usage)
        # Exact match only (case-insensitive)
        groups = signal.list_groups()
        g = None
        gn_lower = group_name.lower()
        # Try exact match first, then substring
        for grp in groups:
            if grp.group_name.lower() == gn_lower:
                g = grp
                break
        if not g:
            for grp in groups:
                if gn_lower in grp.group_name.lower():
                    g = grp
                    break
        if not g:
            linked_names = [grp.group_name for grp in groups if grp.group_id in set(get_admin_group_ids(db, admin_id))]
            names_str = ", ".join(linked_names) if linked_names else ("(немає)" if _tag_lang == "uk" else "(none)")
            if _tag_lang == "uk":
                _send_direct_or_cleanup(admin_id, f"Групу не знайдено: \"{group_name}\"\nВаші прив'язані групи: {names_str}")
            else:
                _send_direct_or_cleanup(admin_id, f"Group not found: \"{group_name}\"\nYour linked groups: {names_str}")
            return
        admin_group_ids = set(get_admin_group_ids(db, admin_id))
        if g.group_id not in admin_group_ids:
            if _tag_lang == "uk":
                _send_direct_or_cleanup(admin_id, f"Ви ще не прив'язали групу \"{group_name}\".")
            else:
                _send_direct_or_cleanup(admin_id, f"You haven't linked group \"{group_name}\" yet.")
            return
        set_tag_targets(db, g.group_id, phones)
        phones_str = ", ".join(phones)
        if _tag_lang == "uk":
            _send_direct_or_cleanup(admin_id, f"Цілі тегування для \"{group_name}\" встановлено: {phones_str}")
        else:
            _send_direct_or_cleanup(admin_id, f"Tag targets for \"{group_name}\" set to: {phones_str}")
        return

    # Ignore commands other than language to prevent accidental group searches
    if text.startswith("/"):
        msg = "Unknown command. Available: /en, /ua, /wipe, /union, /split, /tag"
        _send_direct_or_cleanup(admin_id, msg)
        return
    # -----------------------------

    # No session → create one and send welcome
    if session is None:
        detected_lang = _detect_language(text)
        set_admin_awaiting_group_name(db, admin_id)
        set_admin_lang(db, admin_id, detected_lang)
        log.info("New admin %s, detected language: %s, sending welcome", admin_id, detected_lang)
        if not isinstance(signal, NoopSignalAdapter):
            sent = signal.send_onboarding_prompt(recipient=admin_id, lang=detected_lang)
            if not sent:
                from app.db.queries_mysql import unlink_admin_from_all_groups
                delete_admin_session(db, admin_id)
                unlink_admin_from_all_groups(db, admin_id)
                log.info("Cleared session for blocked/removed user %s", admin_id)
        return  # Wait for next message with group name

    lang = session.lang

    if not text:
        # Attachment-only or sticker messages have no text — ignore silently
        if m.image_paths:
            log.info("Admin %s sent attachment-only message (no text), ignoring", admin_id)
            return
        # Truly empty message (no text, no attachments) — ignore, don't spam welcome
        log.info("Admin %s sent empty message, ignoring", admin_id)
        return
    
    # Any message while a job is running cancels it and restarts.
    if session is not None and session.state == "awaiting_qr_scan" and session.pending_token:
        log.info(
            "Admin %s sent message while job is running — cancelling job %s and restarting",
            admin_id, session.pending_token,
        )
        cancel_all_history_jobs_for_admin(db, admin_id)
        set_admin_awaiting_group_name(db, admin_id)
        lang = getattr(session, "lang", "uk") or "uk"
        if lang == "en":
            _send_direct_or_cleanup(admin_id, "Ingestion cancelled. Send a group name to start again.")
        else:
            _send_direct_or_cleanup(admin_id, "Імпорт скасовано. Надішліть назву групи, щоб почати знову.")
        return

    # Try to find group by name
    if isinstance(signal, NoopSignalAdapter):
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

    # Verify sender is a member of this group (superadmins bypass this check)
    is_superadmin = admin_id in settings.superadmin_list
    if group.members and admin_id not in group.members and not is_superadmin:
        log.warning("User %s is NOT a member of group '%s' — rejecting", admin_id, group.group_name)
        reject_msg = (
            f"Ви не є учасником групи \"{group.group_name}\". Тільки учасники групи можуть запускати імпорт історії."
            if lang == "uk" else
            f"You are not a member of group \"{group.group_name}\". Only group members can initiate history import."
        )
        _send_direct_or_cleanup(admin_id, reject_msg)
        return
    if is_superadmin:
        log.info("Superadmin %s bypassing membership check for group '%s'", admin_id, group.group_name)

    # INSTANT FEEDBACK: Tell user we found it and generating QR
    signal.send_processing_message(recipient=admin_id, group_name=group.group_name, lang=lang)

    # Link admin to group IMMEDIATELY so bot can respond in the group
    # even if the QR/history import fails or is skipped.
    link_admin_to_group(db, admin_id=admin_id, group_id=group.group_id)
    log.info("Admin %s linked to group %s (pre-QR)", admin_id, group.group_id)

    # Auto-populate tag targets with all whitelisted phone numbers
    if settings.admin_whitelist:
        from app.db.queries_mysql import set_tag_targets
        default_targets = [p for p in settings.admin_whitelist if p.startswith("+")]
        if default_targets:
            set_tag_targets(db, group.group_id, default_targets)

    # Check if another admin is already ingesting this group
    from app.db.queries_mysql import get_active_history_job_for_group
    active_job = get_active_history_job_for_group(db, group.group_id)
    if active_job and active_job != admin_id:
        log.info("Group %s already being ingested by %s — rejecting %s", group.group_id[:20], active_job[:20], admin_id)
        if lang == "en":
            _send_direct_or_cleanup(admin_id, f"Group \"{group.group_name}\" is already being ingested by another admin. Please wait for it to finish.")
        else:
            _send_direct_or_cleanup(admin_id, f"Група \"{group.group_name}\" вже імпортується іншим адміністратором. Зачекайте, поки процес завершиться.")
        return

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
        on_message_stored=_debouncer.on_message,
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

        if r.emoji in POSITIVE_EMOJI:
            # Update buffer text so the next extraction sees the reaction count
            _update_buffer_reaction_count(r.group_id, r.target_ts)

            # Find the message being reacted to; enqueue BUFFER_UPDATE so the
            # LLM re-evaluates the buffer with the updated reaction count.
            from app.db import get_message_by_ts
            target_msg = get_message_by_ts(db, group_id=r.group_id, ts=r.target_ts)
            if target_msg:
                msg_id = target_msg.message_id
                # If the reacted message is a bot reply, use the original message
                # it was replying to for the BUFFER_UPDATE trigger.
                if target_msg.reply_to_id:
                    msg_id = target_msg.reply_to_id
                enqueue_job(db, BUFFER_UPDATE, {"group_id": r.group_id, "message_id": msg_id})
                log.info("Enqueued BUFFER_UPDATE after reaction on ts=%s (msg_id=%s)", r.target_ts, msg_id)

            n = confirm_cases_by_evidence_ts(
                db, group_id=r.group_id, target_ts=r.target_ts, emoji=r.emoji
            )
            if n:
                log.info(
                    "Case confirmation via emoji %s on ts=%s in group=%s: %d case(s) updated",
                    r.emoji, r.target_ts, r.group_id, n,
                )


def _update_buffer_reaction_count(group_id: str, target_ts: int) -> None:
    """Update the reaction count in the buffer text for a specific message."""
    import re as _re
    from app.db import get_buffer, set_buffer, get_positive_reactions_for_message

    buf = get_buffer(db, group_id=group_id)
    if not buf:
        return

    new_count = get_positive_reactions_for_message(db, group_id=group_id, target_ts=target_ts)
    if new_count <= 0:
        return

    ts_str = str(target_ts)
    lines = buf.split("\n")
    updated = False
    for i, line in enumerate(lines):
        if f"ts={ts_str}" not in line:
            continue
        # Replace existing reactions=N or add it before the newline
        if _re.search(r'\breactions=\d+', line):
            lines[i] = _re.sub(r'\breactions=\d+', f'reactions={new_count}', line)
        else:
            lines[i] = line.rstrip() + f" reactions={new_count}"
        updated = True
        break

    if updated:
        set_buffer(db, group_id=group_id, buffer_text="\n".join(lines))
        log.debug("Buffer updated: reactions=%d on ts=%s in group=%s", new_count, ts_str, group_id[:20])


def _send_direct_or_cleanup(admin_id: str, text: str) -> bool:
    """Send a direct message to an admin; trigger contact-removed cleanup on failure.

    Signal Desktop (and signal-cli) return False when the recipient has blocked
    or removed the bot.  Any adapter other than NoopSignalAdapter is expected to
    return a meaningful bool, so we use the failure to eagerly clean up state.
    Returns True if the message was sent successfully.
    """
    sent = signal.send_direct_text(recipient=admin_id, text=text)
    if not sent and not isinstance(signal, NoopSignalAdapter):
        log.info("send_direct_text to %s failed — triggering contact-removed cleanup", admin_id)
        _handle_contact_removed(admin_id)
    return sent


def _handle_group_update(group_id: str) -> None:
    """Handle group metadata changes (description, name, avatar).

    Enqueues SYNC_GROUP_DOCS so it runs in queue order before any message
    handling — description sync first, then answering.
    """
    log.info("Group update event for %s — enqueuing docs sync", group_id[:20])
    enqueue_job(db, SYNC_GROUP_DOCS, {"group_id": group_id})


def _handle_remote_delete(event) -> None:
    """Handle when a user deletes a message in a group — remove it from our DB."""
    from app.db.queries_mysql import delete_raw_message_by_ts
    deleted = delete_raw_message_by_ts(db, event.group_id, event.deleted_ts)
    if deleted:
        log.info("Remote delete: removed message ts=%s from group %s", event.deleted_ts, event.group_id[:20])


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


def _notify_interrupted_ingestions() -> None:
    """On startup, find admins stuck in awaiting_qr_scan and notify them.

    If we restarted mid-ingestion (deploy), the ingest process is dead but
    the admin session still shows awaiting_qr_scan. Reset them and send a message.
    """
    time.sleep(10)  # Wait for signal listener to start
    try:
        with db.connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT admin_id, pending_group_name, lang FROM admin_sessions WHERE state = 'awaiting_qr_scan'"
            )
            rows = cur.fetchall()
        if not rows:
            return
        log.info("Found %d interrupted ingestion sessions on startup", len(rows))
        for admin_id, group_name, lang in rows:
            set_admin_awaiting_group_name(db, admin_id)
            if lang == "en":
                msg = f"Ingestion of \"{group_name or 'group'}\" was interrupted by a server restart. Please send the group name again to retry."
            else:
                msg = f"Імпорт \"{group_name or 'групи'}\" було перервано перезапуском сервера. Надішліть назву групи ще раз, щоб повторити."
            _send_direct_or_cleanup(admin_id, msg)
            log.info("Notified admin %s about interrupted ingestion for '%s'", admin_id[:20], group_name)
    except Exception:
        log.exception("Failed to notify interrupted ingestions on startup")


@app.on_event("startup")
def _startup() -> None:
    _r2.init_r2()

    t = threading.Thread(target=worker_loop_forever, args=(deps,), daemon=True)
    t.start()

    def _admin_reconcile_loop() -> None:
        while True:
            try:
                _prune_disconnected_admins()
            except Exception:
                log.exception("Admin reconcile loop failed")
            time.sleep(600)  # Prune every 10 min — signal-cli locks config file, starving receive loop if frequent. DMs trigger prune on-demand anyway.

    threading.Thread(target=_admin_reconcile_loop, daemon=True).start()

    # Start listener only if the account is already linked/registered.
    _maybe_start_signal_listener()

    # Notify admins whose ingestion was interrupted by a deploy/restart.
    # They have state='awaiting_qr_scan' but the ingest process is dead.
    threading.Thread(target=_notify_interrupted_ingestions, daemon=True).start()

    log.info("Startup complete")


@app.get("/signal/link-device/qr")
def signal_link_device_qr() -> Response:
    """
    Debug-only: generate and return a QR PNG for `signal-cli link`.

    Open this endpoint in a desktop browser and scan the QR on your phone:
      Signal -> Profile Icon -> Linked devices -> Link new device
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


import re as _re_mod

_OCR_PATTERNS = [
    _re_mod.compile(r'\n*\[Зображення:[^\]]*\]'),
    _re_mod.compile(r'\n*\[Зображення\]'),
    _re_mod.compile(r'\n*\[Відео:[^\]]*\]'),
    _re_mod.compile(r'\n*\[Транскрипт відео:[^\]]*\]'),
    _re_mod.compile(r'\n*\[attachment:[^\]]*\]'),
    _re_mod.compile(r'\n*\[image\]\s*\{[^}]*\}'),
    _re_mod.compile(r'\n*\[image\]'),
    _re_mod.compile(r'\{"extracted_text"\s*:[^}]*\}'),
]


def _strip_ocr_markers(text: str) -> str:
    """Remove OCR/media markers from message text (AI-only metadata)."""
    for pat in _OCR_PATTERNS:
        text = pat.sub('', text)
    return text.strip()


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
        attachments = []
        for p in msg.image_paths:
            if p.startswith("/var/lib/signal/"):
                url = p.replace("/var/lib/signal/", "/static/")
            else:
                url = p
            mime, _ = mimetypes.guess_type(url)
            attachments.append({"url": url, "content_type": mime or "application/octet-stream"})

        # backward-compat: keep flat "images" list of URLs
        flat_urls = [a["url"] for a in attachments]

        # Strip OCR/media markers from content_text (AI-only metadata)
        clean_text = _strip_ocr_markers(msg.content_text or "")

        evidence_data.append({
            "message_id": msg.message_id,
            "ts": msg.ts,
            "sender_hash": msg.sender_hash,
            "sender_name": msg.sender_name,
            "content_text": clean_text,
            "images": flat_urls,
            "attachments": attachments,
            "reply_to_id": msg.reply_to_id,
        })

    case["evidence"] = evidence_data
    return case


@app.get("/api/group-cases")
def list_group_cases(group_id: str, include_archived: bool = False):
    """List cases for a group (non-archived by default). Pass group_id as query param."""
    from app.db import get_cases_for_group
    cases = get_cases_for_group(db, group_id, include_archived=include_archived)
    return {"group_id": group_id, "cases": cases}


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


def _path_to_url(p: str) -> str:
    """Convert a storage path to a URL. R2 URLs are returned as-is."""
    if p.startswith("https://") or p.startswith("http://"):
        return p
    if p.startswith("/var/lib/signal/"):
        return p.replace("/var/lib/signal/", "/static/", 1)
    return p


def _format_content_html(content_text: str) -> str:
    """Format content_text for case page display.

    Parses markers like [Відео: ...], [Транскрипт відео: ...], [Зображення: ...],
    [image], and raw JSON OCR results into clean HTML.
    """
    import html as _html
    import re
    import json

    if not content_text:
        return ""

    text = content_text

    # Try to parse and clean up raw JSON OCR results embedded in the text
    def _clean_json_block(match):
        raw = match.group(0)
        try:
            parsed = json.loads(raw)
            parts = []
            et = (parsed.get("extracted_text") or "").strip()
            desc = (parsed.get("description") or "").strip()
            if et:
                parts.append(f"Текст: {et}")
            if desc:
                parts.append(f"Опис: {desc}")
            return " | ".join(parts) if parts else raw
        except Exception:
            return raw

    text = re.sub(r'\{["\s]*"extracted_text"[^}]+\}', _clean_json_block, text)

    lines = text.split("\n")
    result_parts = []
    skip_image_marker = False

    for line in lines:
        stripped = line.strip()

        if stripped == "[image]":
            skip_image_marker = True
            continue

        # Video marker: [Відео: filename — desc] or [Відео: filename] — strip silently
        video_match = re.match(r'^\[Відео:\s*(.+?)\]$', stripped)
        if video_match:
            skip_image_marker = False
            continue

        # Transcript marker -> collapsible
        transcript_match = re.match(r'^\[Транскрипт відео:\s*(.+)\]$', stripped)
        if transcript_match:
            transcript_text = _html.escape(transcript_match.group(1))
            result_parts.append(
                f'<details class="transcript"><summary>Транскрипт</summary>'
                f'<div class="transcript-text">{transcript_text}</div></details>'
            )
            skip_image_marker = False
            continue

        # Image OCR marker
        img_match = re.match(r'^\[Зображення:\s*(.+?)\]$', stripped)
        if img_match:
            inner = _html.escape(img_match.group(1))
            result_parts.append(
                f'<details class="ocr-details"><summary>Розпізнаний текст</summary>'
                f'<div class="ocr-text">{inner}</div></details>'
            )
            skip_image_marker = False
            continue

        # Attachment marker
        att_match = re.match(r'^\[attachment:\s*(.+?)\]$', stripped)
        if att_match:
            skip_image_marker = False
            continue

        if stripped:
            result_parts.append(f'<p>{_html.escape(stripped)}</p>')
            skip_image_marker = False

    return "\n".join(result_parts)


def _media_html(paths: list) -> str:
    """Return HTML for a list of media file paths (images / video / fallback download)."""
    import html as _html
    parts = []
    for p in paths:
        url = _path_to_url(p)
        url_esc = _html.escape(url)
        ext = Path(p).suffix.lower()
        if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
            parts.append(
                f'<a href="{url_esc}" target="_blank">'
                f'<img src="{url_esc}" loading="lazy" class="media-img" alt="attachment" /></a>'
            )
        elif ext in (".mp4", ".webm", ".ogg", ".mov"):
            parts.append(
                f'<video controls class="media-video">'
                f'<source src="{url_esc}"><a href="{url_esc}" target="_blank">Download video</a>'
                f'</video>'
            )
        else:
            name = _html.escape(Path(p).name)
            parts.append(f'<a href="{url_esc}" class="media-download" download>⬇ {name}</a>')
    return "\n".join(parts)


@app.get("/case/{case_id}", response_class=HTMLResponse)
def view_case(case_id: str):
    import html as _html

    case = get_case(db, case_id)

    # Fallback: Check static JSON if not in DB
    if not case:
        try:
            import json
            cases_path = Path("data/signal_cases_structured.json")
            if cases_path.exists():
                with open(cases_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if case_id.isdigit():
                        idx = int(case_id)
                        for c in data.get("cases", []):
                            if c.get("idx") == idx:
                                case = {
                                    "problem_title": "Case #" + str(idx),
                                    "status": "solved",
                                    "problem_summary": c.get("problem_summary"),
                                    "solution_summary": c.get("solution_summary"),
                                }
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

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Case {case_id}</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                   max-width: 800px; margin: 0 auto; padding: 20px; color: #333; }}
            .case-header {{ border-bottom: 1px solid #ccc; padding-bottom: 10px; margin-bottom: 20px; }}
            .status {{ display: inline-block; padding: 5px 10px; border-radius: 5px; background: #eee; }}
            .status.solved {{ background: #d4edda; color: #155724; }}
            .case-date {{ color: #666; font-size: 0.9em; margin-top: 4px; }}
            .message {{ border: 1px solid #e0e0e0; padding: 12px; margin-bottom: 12px; border-radius: 8px;
                       background: #fafafa; }}
            .meta {{ color: #888; font-size: 0.85em; margin-bottom: 6px; }}
            .content p {{ margin: 4px 0; }}
            .media-label {{ color: #555; font-size: 0.9em; margin: 6px 0; }}
            .media-img {{ max-width: 100%; height: auto; margin-top: 10px; border-radius: 4px; }}
            .media-video {{ max-width: 100%; margin-top: 10px; border-radius: 4px; }}
            details.transcript, details.ocr-details {{
                margin: 8px 0; border: 1px solid #e0e0e0; border-radius: 6px;
                background: #fff; overflow: hidden;
            }}
            details.transcript summary, details.ocr-details summary {{
                padding: 7px 12px; cursor: pointer; font-weight: 500; font-size: 0.85em;
                color: #666; background: #f6f7f9; user-select: none; list-style: none;
            }}
            details.transcript summary::-webkit-details-marker {{ display: none; }}
            details.transcript summary::before, details.ocr-details summary::before {{
                content: '\\25B8  '; font-size: 10px;
            }}
            details.transcript[open] summary::before, details.ocr-details[open] summary::before {{
                content: '\\25BE  ';
            }}
            details.transcript summary:hover, details.ocr-details summary:hover {{
                color: #333;
            }}
            .transcript-text, .ocr-text {{
                padding: 10px 12px; white-space: pre-wrap; font-size: 0.9em;
                line-height: 1.6; color: #222; border-top: 1px solid #e0e0e0;
            }}
        </style>
    </head>
    <body>
        <div class="case-header">
            <h1>{_html.escape(case.get('problem_title', 'Case ' + case_id))}</h1>
            <div class="status {case.get('status', 'recommendation')}">{case.get('status', 'recommendation')}</div>
            <div class="case-date">Created: {_html.escape(case.get('created_at', '') or 'Unknown')}</div>
            <p><strong>Problem:</strong> {_html.escape(case.get('problem_summary', ''))}</p>
            <p><strong>Solution:</strong> {_html.escape(case.get('solution_summary', ''))}</p>
        </div>

        <h2>Evidence</h2>
        <div class="evidence-list">
    """

    if evidence:
        from datetime import datetime, timezone
        try:
            from zoneinfo import ZoneInfo
            _tz_kyiv = ZoneInfo("Europe/Kyiv")
        except ImportError:
            from datetime import timedelta
            _tz_kyiv = timezone(timedelta(hours=2))
        for msg in evidence:
            dt = datetime.fromtimestamp(msg.ts / 1000, tz=_tz_kyiv)
            ts_str = dt.strftime('%Y-%m-%d %H:%M:%S')
            formatted_content = _format_content_html(msg.content_text or "")
            html += f"""
                <div class="message">
                    <div class="meta">{msg.sender_hash[:8]} at {ts_str}</div>
                    <div class="content">{formatted_content}</div>
            """
            html += _media_html(msg.image_paths)
            html += "</div>"
    else:
        html += "<p>No evidence stored for this case.</p>"

    html += """
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


_WORKER_STALL_THRESHOLD_S = 300  # seconds before we consider the worker stalled


@app.get("/healthz")
def healthz() -> dict:
    age = get_worker_heartbeat_age()
    if age > _WORKER_STALL_THRESHOLD_S:
        raise HTTPException(
            status_code=503,
            detail=f"Worker stalled: no heartbeat for {age:.0f}s",
        )
    return {"ok": True, "worker_heartbeat_age_s": round(age, 1)}


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
            return "Зберігаю кейси в базу знань..."
        else:
            return "Saving cases to knowledge base..."
    
    elif key == "qr_sent":
        # QR code was sent separately with instructions, no additional message needed
        return ""

    elif key == "qr_reminder":
        if lang == "uk":
            return "Ще 3 хвилини для сканування QR-коду. Не забудьте відсканувати!"
        return "3 minutes left to scan the QR code. Don't forget to scan it!"

    elif key == "syncing":
        if lang == "uk":
            return "Синхронізація Signal... зачекайте, це може зайняти кілька хвилин."
        return "Syncing Signal... please wait, this may take a few minutes."

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
        _send_direct_or_cleanup(admin_id, progress_text)
    
    return {"ok": True}


class HistoryQrCodeRequest(BaseModel):
    """Called by signal-ingest to send QR code image to user."""
    token: str
    qr_image_base64: str
    is_refresh: bool = False
    remaining_seconds: int = 0


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

        # First QR: full instructions. Refreshed QR: short caption with countdown.
        if req.is_refresh:
            remaining_min = max(1, req.remaining_seconds // 60)
            if lang == "uk":
                caption = f"Оновлений QR-код (попередній закінчився). Скануйте одразу! Залишилось {remaining_min} хв."
            else:
                caption = f"Refreshed QR code (previous one expired). Scan now! {remaining_min} min left."
        else:
            if lang == "uk":
                caption = (
                    "Відскануйте цей QR-код у Signal:\n\n"
                    "1. Відкрийте Signal на телефоні\n"
                    "2. Іконка профілю → Пов'язані пристрої → Додати пристрій\n"
                    "3. Відскануйте QR-код\n"
                    "4. Натисніть «Перенести історію повідомлень» (Transfer message history)\n\n"
                    "Примітка: макс. 5 пов'язаних пристроїв. Видаліть один при ліміті.\n\n"
                    "QR-код дійсний ~1 хв. Якщо не встигнете — надішлю новий автоматично. Залишилось 5 хв."
                )
            else:
                caption = (
                    "Scan this QR code in Signal:\n\n"
                    "1. Open Signal on your phone\n"
                    "2. Profile Icon → Linked Devices → Link New Device\n"
                    "3. Scan the QR code\n"
                    "4. Click \"Transfer message history\"\n\n"
                    "Note: max 5 linked devices. Remove one if limit reached.\n\n"
                    "QR code valid for ~1 min. If it expires, a new one will be sent automatically. 5 min left."
                )

        if not isinstance(signal, NoopSignalAdapter):
            signal.send_direct_image(recipient=admin_id, image_path=qr_path, caption=caption,
                                     retries=4, retry_delay=8.0)

        # Clean up temp file
        os.unlink(qr_path)
        log.info("QR code sent to %s (refresh=%s)", admin_id, req.is_refresh)
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
                    # Send import summary first (if available)
                    if req.message_count is not None or req.cases_inserted is not None or req.note:
                        if lang == "uk":
                            summary = (
                                f"Підсумок імпорту: повідомлень={req.message_count if req.message_count is not None else '?'}"
                                f", кейсів додано={req.cases_inserted if req.cases_inserted is not None else '?'}."
                            )
                        else:
                            summary = (
                                f"Import summary: messages={req.message_count if req.message_count is not None else '?'}"
                                f", cases_added={req.cases_inserted if req.cases_inserted is not None else '?'}."
                            )
                        if req.note:
                            summary += f"\n{req.note}"
                        _send_direct_or_cleanup(admin_id, summary)
                    # Send success message last, with website link
                    signal.send_success_message(recipient=admin_id, group_name=group_name, lang=lang)
                else:
                    signal.send_failure_message(recipient=admin_id, group_name=group_name, lang=lang)
            except Exception:
                log.exception("Failed to send link-result notification to admin %s", admin_id)

        threading.Thread(target=_send_notifications, daemon=True).start()

    return {"ok": True}


class HistoryImagePayload(BaseModel):
    """A single image attachment encoded as base64, sent from signal-ingest."""
    filename: str = ""
    content_type: str = "image/jpeg"
    data_b64: str  # base64-encoded image bytes


class HistoryMessage(BaseModel):
    message_id: str
    sender_hash: str
    sender_name: str | None = None
    ts: int
    content_text: str
    image_payloads: List[HistoryImagePayload] = []
    reply_to_id: str | None = None


class CaseBlock(BaseModel):
    case_block: str


class StructuredCaseBlock(BaseModel):
    """Pre-structured case from optimized ingest pipeline (skips make_case)."""
    case_block: str
    problem_title: str = ""
    problem_summary: str = ""
    solution_summary: str = ""
    status: str = "solved"
    tags: List[str] = []
    evidence_ids: List[str] = []


class HistoryCasesRequest(BaseModel):
    token: str
    group_id: str
    cases: List[CaseBlock] = []
    cases_structured: List[StructuredCaseBlock] = []  # When present, used instead of cases (8x fewer API calls)
    messages: List[HistoryMessage] = []  # Optional: raw messages for evidence linking


def _save_history_images(
    group_id: str,
    message_id: str,
    image_payloads: list,
    storage_root: str,
) -> list:
    """Decode base64 attachment payloads and store them.

    If R2 is configured, uploads to R2 and returns public URLs.
    Otherwise saves to disk under ``<storage_root>/history/<group_id>/``
    and returns absolute paths.
    Uploads run in parallel when R2 is enabled for speed.
    """
    import base64 as _b64
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

    if not image_payloads:
        return []

    def _ext_for(filename: str, content_type: str) -> str:
        ext = Path(filename).suffix if filename else ""
        if ext:
            return ext
        ct = (content_type or "").split(";")[0].strip().lower()
        guessed = mimetypes.guess_extension(ct)
        if guessed:
            return guessed
        return ".bin"

    # Decode all payloads first
    items = []
    for i, img in enumerate(image_payloads):
        try:
            raw_bytes = _b64.b64decode(img.data_b64)
        except Exception as e:
            log.warning("Failed to decode payload for %s[%d]: %s", message_id, i, e)
            continue
        ext = _ext_for(img.filename or "", img.content_type or "")
        content_type = (img.content_type or "application/octet-stream").split(";")[0].strip()
        safe_msg_id = message_id.replace("/", "_").replace(":", "_")
        filename = f"{safe_msg_id}_{i}{ext}"
        items.append((raw_bytes, content_type, filename, i))

    if not items:
        return []

    if _r2.is_enabled():
        results = [None] * len(items)

        def _upload_one(idx, raw_bytes, content_type, filename):
            key = f"history/{group_id}/{filename}"
            try:
                url = _r2.upload(key, raw_bytes, content_type)
                return idx, url, key
            except Exception:
                return idx, None, key

        with ThreadPoolExecutor(max_workers=min(len(items), 8)) as pool:
            futs = [pool.submit(_upload_one, j, rb, ct, fn) for j, (rb, ct, fn, _) in enumerate(items)]
            for fut in _as_completed(futs):
                idx, url, key = fut.result()
                if url:
                    results[idx] = url
                else:
                    log.error("R2 upload failed for %s, falling back to disk", key)
                    rb, ct, fn, _ = items[idx]
                    dest_dir = Path(storage_root) / "history" / group_id
                    try:
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        dest_file = dest_dir / fn
                        dest_file.write_bytes(rb)
                        results[idx] = str(dest_file)
                    except Exception as e2:
                        log.warning("Local save also failed for %s: %s", fn, e2)

        return [r for r in results if r is not None]

    # Local disk only
    results: list = []
    dest_dir = Path(storage_root) / "history" / group_id
    for raw_bytes, content_type, filename, _ in items:
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_file = dest_dir / filename
            dest_file.write_bytes(raw_bytes)
            results.append(str(dest_file))
        except Exception as e:
            log.warning("Failed to save attachment %s: %s", filename, e)
    return results


def _init_buffer_from_history(group_id: str, max_messages: int = 300) -> None:
    """Seed the message buffer with the most recent messages from history import.

    Uses the full buffer window (300 messages) so the bot has complete context
    for live case extraction immediately after ingestion.
    """
    from app.db import set_buffer, get_positive_reactions_for_message
    from app.db.queries_mysql import get_recent_raw_messages
    from app.jobs.worker import _format_buffer_line

    messages = get_recent_raw_messages(db, group_id=group_id, limit=max_messages)
    if not messages:
        return

    lines = []
    for msg in messages:
        positive_reactions = get_positive_reactions_for_message(db, group_id=group_id, target_ts=msg.ts)
        line = _format_buffer_line(msg, positive_reactions=positive_reactions)
        lines.append(line)

    buf = "".join(lines)
    set_buffer(db, group_id=group_id, buffer_text=buf)
    log.info("Initialized buffer with %d messages for group %s", len(messages), group_id[:20])


def _process_history_cases_bg(req: HistoryCasesRequest) -> int:
    """Process history cases synchronously. Returns number of cases inserted.

    Transactional: existing cases are only archived AFTER new cases are
    successfully inserted.  If ingestion fails midway, old cases and RAG
    entries remain intact so the group is never left empty.
    """
    import re
    from app.db import RawMessage, mark_case_in_rag
    from app.db.queries_mysql import archive_cases_for_group, clear_group_runtime_data, set_group_ingesting

    # Collect existing case IDs BEFORE inserting new ones — we'll archive
    # them only after the new cases are successfully stored.
    existing_case_ids: list[str] = []
    try:
        with db.connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT case_id FROM cases WHERE group_id = %s AND status != 'archived'",
                (req.group_id,),
            )
            existing_case_ids = [r[0] for r in cur.fetchall()]
        log.info("Existing cases to archive after success: %d (group=%s)", len(existing_case_ids), req.group_id[:20])
    except Exception:
        log.exception("Failed to list existing cases for group %s", req.group_id[:20])

    # NOTE: We do NOT clear buffer/reactions/messages upfront.
    # Everything is cleaned up only after successful ingestion (transactional).
    # insert_raw_message handles duplicates gracefully (INSERT + dup check).

    # Store raw messages for evidence linking — parallelise R2 uploads
    import time as _time
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

    msg_store_start = _time.time()
    message_lookup: dict = {}
    messages_stored = 0

    def _store_one_message(m):
        image_paths = _save_history_images(
            group_id=req.group_id,
            message_id=m.message_id,
            image_payloads=m.image_payloads,
            storage_root=settings.signal_bot_storage,
        )
        raw_msg = RawMessage(
            message_id=m.message_id,
            group_id=req.group_id,
            ts=m.ts,
            sender_hash=m.sender_hash,
            sender_name=m.sender_name,
            content_text=m.content_text,
            image_paths=image_paths,
            reply_to_id=m.reply_to_id,
        )
        return m, raw_msg

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(_store_one_message, m) for m in req.messages]
        for fut in _as_completed(futs):
            m, raw_msg = fut.result()
            if insert_raw_message(db, raw_msg):
                messages_stored += 1
            content_key = m.content_text.strip()[:100] if m.content_text else ""
            if content_key:
                message_lookup[content_key] = m.message_id
            message_lookup[str(m.ts)] = m.message_id

    if messages_stored > 0:
        log.info("Stored %d raw messages in %.1fs (group=%s)", messages_stored, _time.time()-msg_store_start, req.group_id[:20])

    # Support both legacy (cases + make_case per item) and optimized (cases_structured + batch embed)
    use_structured = bool(req.cases_structured)
    case_items = req.cases_structured if use_structured else [{"case_block": c.case_block} for c in req.cases]

    if not case_items:
        log.info("No cases to process")
        return 0, []

    # Batch embed for structured pipeline (8x fewer API calls)
    dedup_embeddings: List[List[float]] = []
    if use_structured:
        embed_start = _time.time()
        dedup_texts = [f"{c.problem_title}\n{c.problem_summary}" for c in req.cases_structured]
        dedup_embeddings = llm.embed_batch(texts=dedup_texts)
        log.info("Batch embedding %d cases took %.1fs", len(dedup_texts), _time.time()-embed_start)

    cases_start = _time.time()
    kept = 0
    final_case_ids: List[str] = []
    for idx, c in enumerate(case_items):
        try:
            if use_structured:
                sc = req.cases_structured[idx]
                case = type("Case", (), {
                    "keep": True,
                    "status": sc.status or "solved",
                    "problem_title": sc.problem_title or "",
                    "problem_summary": sc.problem_summary or "",
                    "solution_summary": sc.solution_summary or "",
                    "tags": list(sc.tags) if sc.tags else [],
                    "evidence_ids": list(sc.evidence_ids) if sc.evidence_ids else [],
                })()
                case_block = sc.case_block
                dedup_embedding = dedup_embeddings[idx]
            else:
                case = llm.make_case(case_block_text=c["case_block"])
                if not case.keep:
                    continue
                case_block = c["case_block"]
                embed_text = f"{case.problem_title}\n{case.problem_summary}"
                dedup_embedding = llm.embed(text=embed_text)

            # Build evidence_ids: prefer LLM-extracted, fallback to content matching
            evidence_ids = list(case.evidence_ids) if hasattr(case, "evidence_ids") else []
            if not evidence_ids:
                for line in case_block.split('\n'):
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

            # Detect emoji-confirmed cases: case_block header lines contain reactions=N
            emoji_confirmed = bool(re.search(r'\breactions=\d+', case_block))
            rxn_emoji_match = re.search(r'\breaction_emoji=(\S+)', case_block)
            confirmed_emoji = rxn_emoji_match.group(1).rstrip('])"\',') if rxn_emoji_match else "👍"

            evidence_image_paths: List[str] = []
            for mid in evidence_ids:
                msg = get_raw_message(db, message_id=mid)
                if msg:
                    evidence_image_paths.extend(p for p in msg.image_paths if p)

            # Semantic dedup: find existing case with cosine similarity > threshold
            similar_id = find_similar_case(db, group_id=req.group_id, embedding=dedup_embedding)
            log.info(
                "Dedup check for '%s': similar_id=%s",
                getattr(case, "problem_title", "?")[:40], similar_id,
            )
            if similar_id:
                merge_case(
                    db,
                    target_case_id=similar_id,
                    status=case.status,
                    problem_title=case.problem_title,
                    problem_summary=case.problem_summary,
                    solution_summary=case.solution_summary,
                    tags=case.tags,
                    evidence_ids=evidence_ids,
                    evidence_image_paths=evidence_image_paths,
                )
                # Keep the original case's embedding — don't overwrite with
                # the merged case's embedding to prevent attractor drift.
                case_id = similar_id
                action = "semantic-merged"
            else:
                # Fallback: exact-title dedup via upsert_case
                case_id, created = upsert_case(
                    db,
                    case_id=new_case_id(db),
                    group_id=req.group_id,
                    status=case.status,
                    problem_title=case.problem_title,
                    problem_summary=case.problem_summary,
                    solution_summary=case.solution_summary,
                    tags=case.tags,
                    evidence_ids=evidence_ids,
                    evidence_image_paths=evidence_image_paths,
                )
                store_case_embedding(db, case_id, dedup_embedding)
                action = "inserted" if created else "title-deduped"

            # For emoji-confirmed cases set closed_emoji if not already set by a live reaction
            if emoji_confirmed and case.status == "solved":
                with db.connection() as _conn:
                    _conn.cursor().execute(
                        "UPDATE cases SET closed_emoji = %s WHERE case_id = %s AND closed_emoji IS NULL",
                        (confirmed_emoji, case_id),
                    )
                    _conn.commit()

            log.info(
                "History case %s status=%s evidence_ids=%d action=%s (group=%s)",
                case_id, case.status, len(evidence_ids), action, req.group_id[:20],
            )

            # Index solved cases into SCRAG, recommendation cases into RCRAG
            if case.status in ("solved", "recommendation") and case.solution_summary.strip():
                prefix = "[SOLVED]" if case.status == "solved" else "[РЕКОМЕНДАЦІЯ]"
                doc_text = "\n".join([
                    f"{prefix} {case.problem_title.strip()}",
                    f"Проблема: {case.problem_summary.strip()}",
                    f"Рішення: {case.solution_summary.strip()}",
                    "tags: " + ", ".join(case.tags),
                ]).strip()
                rag_embedding = llm.embed(text=doc_text)
                metadata: dict = {"group_id": req.group_id, "status": case.status}
                if evidence_ids:
                    metadata["evidence_ids"] = evidence_ids
                if evidence_image_paths:
                    metadata["evidence_image_paths"] = evidence_image_paths
                rag.upsert_case(case_id=case_id, document=doc_text, embedding=rag_embedding, metadata=metadata, status=case.status)
                mark_case_in_rag(db, case_id)
                collection = "SCRAG" if case.status == "solved" else "RCRAG"
                log.info("Indexed history case %s in %s action=%s (group=%s)",
                         case_id, collection, action, req.group_id[:20])
            else:
                log.info("Stored history case %s (no solution, not indexed) action=%s (group=%s)", case_id, action, req.group_id[:20])

            kept += 1
            final_case_ids.append(case_id)
        except Exception:
            log.exception("Failed to process history case block")
            continue

    # Index the newly extracted cases into RAG.  Old cases are still in RAG
    # at this point — they'll be wiped after this succeeds (transactional safety).
    reindexed = 0
    try:
        from app.db.queries_mysql import get_case
        for cid in final_case_ids:
            gc = get_case(db, cid)
            if not gc:
                continue
            if gc.get("status") == "archived":
                continue
            case_status = gc.get("status", "solved")
            sol = (gc.get("solution_summary") or "").strip()
            if not sol:
                continue
            prefix = "[SOLVED]" if case_status == "solved" else "[РЕКОМЕНДАЦІЯ]"
            doc_text = "\n".join([
                f"{prefix} {(gc.get('problem_title') or '').strip()}",
                f"Проблема: {(gc.get('problem_summary') or '').strip()}",
                f"Рішення: {sol}",
                "tags: " + ", ".join(gc.get("tags") or []),
            ]).strip()
            rag_emb = llm.embed(text=doc_text)
            rag.upsert_case(
                case_id=cid,
                document=doc_text,
                embedding=rag_emb,
                metadata={"group_id": req.group_id, "status": case_status},
                status=case_status,
            )
            mark_case_in_rag(db, cid)
            reindexed += 1
    except Exception:
        log.exception("Failed to index ingest cases for group %s", req.group_id[:20])
    log.info(
        "History cases done: inserted=%d re-indexed_to_scrag=%d group=%s (case loop %.1fs)",
        kept, reindexed, req.group_id[:20], _time.time()-cases_start,
    )

    # SWAP: lock group, archive old data, rebuild buffer, unlock.
    # This is the only window where worker jobs are deferred (~milliseconds).
    if kept > 0 and existing_case_ids:
        set_group_ingesting(db, req.group_id, True)
        try:
            archived = archive_cases_for_group(db, req.group_id, exclude_case_ids=set(final_case_ids))
            log.info("Archived %d old cases for group %s (post-ingest)", archived, req.group_id[:20])
        except Exception:
            log.exception("Failed to archive old cases for group %s", req.group_id[:20])
        try:
            deleted_from_rag = rag.delete_cases(existing_case_ids)
            log.info("Wiped %d old RAG docs for group %s", deleted_from_rag, req.group_id[:20])
        except Exception:
            log.exception("Failed to wipe old RAG cases for group %s", req.group_id[:20])
        try:
            clear_group_runtime_data(db, req.group_id)
            log.info("Cleared old messages/buffer/reactions for group %s (post-ingest)", req.group_id[:20])
        except Exception:
            log.exception("Failed to clear runtime data for group %s", req.group_id[:20])
        try:
            _init_buffer_from_history(req.group_id)
        except Exception:
            log.exception("Failed to init buffer from history for group %s", req.group_id[:20])
        set_group_ingesting(db, req.group_id, False)
    elif kept == 0:
        log.warning("No new cases inserted — keeping ALL existing data intact for group %s", req.group_id[:20])

    return kept, final_case_ids


@app.post("/history/cases")
def history_cases(req: HistoryCasesRequest) -> dict:
    if not validate_history_token(db, token=req.token, group_id=req.group_id):
        raise HTTPException(status_code=403, detail="Invalid/expired token")

    # Security: Verify bot is still in the group.
    # If signal adapter can't list groups (e.g. signal-cli not yet linked), we log a warning
    # and allow through — the ingest service already verified admin group membership via
    # Signal Desktop QR, and the one-time token provides authentication.
    # When debug endpoints are enabled a fake/test group_id is allowed through (for testing).
    try:
        current_group_ids = {g.group_id for g in signal.list_groups()}
        if req.group_id not in current_group_ids:
            if settings.http_debug_endpoints_enabled:
                log.warning(
                    "History ingest: bot not in group %s — allowing because debug mode is on",
                    req.group_id[:20],
                )
            else:
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

    if not req.cases_structured and not req.cases:
        raise HTTPException(status_code=400, detail="Either cases or cases_structured must be non-empty")

    n_cases = len(req.cases_structured) if req.cases_structured else len(req.cases)
    n_messages = len(req.messages)
    import time as _hc_time
    hc_start = _hc_time.time()
    log.info("History ingest started: %d cases, %d messages (group=%s)", n_cases, n_messages, req.group_id[:20])
    try:
        inserted, case_ids = _process_history_cases_bg(req)
    except Exception:
        # Ensure ingesting flag is cleared even on unexpected errors
        from app.db.queries_mysql import set_group_ingesting
        set_group_ingesting(db, req.group_id, False)
        raise
    unique_cases = len(set(case_ids))
    log.info("History ingest done: %d/%d cases (%d unique) in %.1fs (group=%s)", inserted, n_cases, unique_cases, _hc_time.time()-hc_start, req.group_id[:20])
    return {"ok": True, "cases_inserted": unique_cases, "case_ids": list(set(case_ids))}


class BackfillImageItem(BaseModel):
    """A single message's image data for backfill."""
    message_id: str
    image_payloads: List[HistoryImagePayload]


class BackfillImagesRequest(BaseModel):
    """Post-ingest image backfill: attach images to messages stored without them."""
    token: str
    group_id: str
    items: List[BackfillImageItem]


@app.post("/history/backfill-images")
def history_backfill_images(req: BackfillImagesRequest) -> dict:
    """Backfill images for raw_messages that were stored without image data.

    Called by signal-ingest after the main ingest completes, when a second-pass
    attachment fetch succeeds for messages that had no bytes on the first pass.
    Updates both raw_messages.image_paths_json and any case evidence that
    references those messages.
    """
    if not validate_history_token(db, token=req.token, group_id=req.group_id):
        raise HTTPException(status_code=403, detail="Invalid/expired token")

    updated = 0
    for item in req.items:
        image_paths = _save_history_images(
            group_id=req.group_id,
            message_id=item.message_id,
            image_payloads=item.image_payloads,
            storage_root=settings.signal_bot_storage,
        )
        if not image_paths:
            continue

        msg = get_raw_message(db, message_id=item.message_id)
        if not msg:
            log.warning("backfill-images: message %s not found in DB", item.message_id)
            continue

        existing = list(msg.image_paths or [])
        merged = existing + [p for p in image_paths if p not in existing]
        if merged == existing:
            continue

        import json as _json
        with db.connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE raw_messages SET image_paths_json = %s WHERE message_id = %s",
                (_json.dumps(merged), item.message_id),
            )
            cur.execute(
                "UPDATE cases SET evidence_image_paths_json = JSON_ARRAY_APPEND("
                "  COALESCE(evidence_image_paths_json, JSON_ARRAY()),"
                "  '$', %s"
                ") WHERE JSON_CONTAINS(evidence_ids_json, %s)",
                (merged[0], _json.dumps(item.message_id)),
            )
            conn.commit()
        updated += 1
        log.info("backfill-images: updated message %s with %d images", item.message_id, len(image_paths))

    log.info("backfill-images: updated %d/%d messages for group %s", updated, len(req.items), req.group_id[:20])
    return {"ok": True, "updated": updated}


class RetrieveRequest(BaseModel):
    group_id: str
    query: str
    k: int = Field(default=5, ge=1, le=20)


@app.post("/retrieve")
def retrieve(req: RetrieveRequest) -> dict:
    if not settings.http_debug_endpoints_enabled:
        raise HTTPException(status_code=404, detail="Not found")
    emb = llm.embed(text=req.query)
    scrag_res = rag.scrag.retrieve_cases(group_id=req.group_id, embedding=emb, k=req.k, status=None)
    rcrag_res = rag.rcrag.retrieve_cases(group_id=req.group_id, embedding=emb, k=req.k, status=None)
    return {"scrag": scrag_res, "rcrag": rcrag_res}


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


class DebugAnswerRequest(BaseModel):
    group_id: str
    question: str
    lang: str = "uk"


@app.post("/debug/answer")
def debug_answer(req: DebugAnswerRequest) -> dict:
    """
    Directly invoke the RAG pipeline and return the synthesized answer.
    Does NOT send anything via Signal — for testing only.
    Requires HTTP_DEBUG_ENDPOINTS_ENABLED=1.
    """
    if not settings.http_debug_endpoints_enabled:
        raise HTTPException(status_code=404, detail="Not found")

    # SCRAG search
    from app.agent.case_search_agent import CaseSearchAgent
    agent = CaseSearchAgent(rag=rag, llm=llm, public_url=settings.public_url.rstrip("/"))
    ctx = agent.search(req.question, group_id=req.group_id, db=db)

    # Load real context from DB (like production flow)
    from app.db import get_last_messages_text
    bot_hash = _bot_sender_hash
    context_msgs = get_last_messages_text(db, req.group_id, n=settings.context_last_n, bot_sender_hash=bot_hash)
    context_text = "\n".join(context_msgs[:-1]) if len(context_msgs) > 1 else ""

    # Synthesize
    case_ans = agent.answer(req.question, group_id=req.group_id, db=db)
    response = ultimate_agent.answer(req.question, group_id=req.group_id, db=db, lang=req.lang, context=context_text)

    # Also check what ultimate_agent's OWN case_agent finds (to diagnose rag isolation issues)
    ua_ctx = ultimate_agent.case_agent.search(req.question, group_id=req.group_id, db=db)
    ua_case_ans = ultimate_agent.case_agent.answer(req.question, group_id=req.group_id, db=db)

    response_text = response.text if hasattr(response, 'text') else str(response)

    return {
        "question": req.question,
        # From the freshly created agent (uses global `rag`)
        "scrag_hits": len(ctx["scrag"]),
        "b3_hits": len(ctx["b3"]),
        "b1_hits": len(ctx["b1"]),
        "case_context": case_ans[:500] if case_ans else "",
        # From ultimate_agent's internal case_agent (uses its own `self.rag`)
        "ua_scrag_hits": len(ua_ctx["scrag"]),
        "ua_case_context": ua_case_ans[:500] if ua_case_ans else "",
        # Final response
        "response": response_text,
        "is_admin_tag": "[[TAG_ADMIN]]" in (response_text or ""),
        "has_case_link": "supportbot.info/case/" in (response_text or ""),
    }


class DebugGateRequest(BaseModel):
    message: str
    context: str = ""


@app.post("/debug/gate")
def debug_gate(req: DebugGateRequest) -> dict:
    """Test the gating LLM directly. Returns consider + tag without any side effects."""
    if not settings.http_debug_endpoints_enabled:
        raise HTTPException(status_code=404, detail="Not found")
    result = llm.decide_consider(message=req.message, context=req.context)
    return {"consider": result.consider, "tag": result.tag}


class DebugSimulateRequest(BaseModel):
    group_id: str
    last_n: int = 10
    lang: str = "uk"


@app.post("/debug/simulate")
def debug_simulate(req: DebugSimulateRequest) -> dict:
    """Simulate the batch responder on the last N messages of a group.

    Treats the last N messages as "unprocessed" and runs the batch gate
    to extract questions, then synthesizes responses. Does NOT send anything.
    """
    if not settings.http_debug_endpoints_enabled:
        raise HTTPException(status_code=404, detail="Not found")

    from app.jobs.batch_responder import process_batch

    result = process_batch(
        group_id=req.group_id,
        db=db,
        llm=llm,
        ultimate_agent=ultimate_agent,
        settings=settings,
        bot_sender_hash=_bot_sender_hash,
        last_n=req.last_n,
        lang=req.lang,
    )

    return {
        "group_id": result.group_id,
        "unprocessed_count": result.unprocessed_count,
        "questions_extracted": result.questions_extracted,
        "gate_raw": result.gate_raw,
        "responses": [
            {
                "question": r.question[:200],
                "message_ids": r.message_ids,
                "reply_to": r.reply_to_message_id,
                "response": r.response_text,
            }
            for r in result.responses
        ],
        "skipped": result.skipped_questions,
    }


class DebugReindexRequest(BaseModel):
    group_id: str
    unarchive: bool = False  # if true, change archived→solved before re-indexing


@app.post("/debug/reindex-group")
def debug_reindex_group(req: DebugReindexRequest) -> dict:
    """Re-index all solved cases for a group into SCRAG (ChromaDB).

    Useful to restore SCRAG after accidental wipe or after manually fixing DB records.
    Pass unarchive=true to first flip archived→solved for cases that still have a solution.
    """
    if not settings.http_debug_endpoints_enabled:
        raise HTTPException(status_code=404, detail="Not found")

    from app.db.queries_mysql import get_case
    from app.db import mark_case_in_rag

    with db.connection() as conn:
        cur = conn.cursor()

        if req.unarchive:
            cur.execute(
                """UPDATE cases SET status='solved'
                   WHERE group_id=%s AND status='archived'
                   AND solution_summary IS NOT NULL AND solution_summary != ''""",
                (req.group_id,),
            )
            unarchived = cur.rowcount
            conn.commit()
        else:
            unarchived = 0

        cur.execute(
            "SELECT case_id FROM cases WHERE group_id=%s AND status='solved'",
            (req.group_id,),
        )
        rows = cur.fetchall()

    case_ids = [r[0] for r in rows]
    indexed = 0
    for cid in case_ids:
        c = get_case(db, cid)
        if not c:
            continue
        sol = (c.get("solution_summary") or "").strip()
        if not sol:
            continue
        doc_text = "\n".join([
            f"[SOLVED] {(c.get('problem_title') or '').strip()}",
            f"Проблема: {(c.get('problem_summary') or '').strip()}",
            f"Рішення: {sol}",
            "tags: " + ", ".join(c.get("tags") or []),
        ]).strip()
        try:
            emb = llm.embed(text=doc_text)
            rag.upsert_case(
                case_id=cid,
                document=doc_text,
                embedding=emb,
                metadata={"group_id": req.group_id, "status": "solved"},
                status="solved",
            )
            mark_case_in_rag(db, cid)
            indexed += 1
        except Exception as exc:
            log.warning("Failed to re-index case %s: %s", cid, exc)

    log.info(
        "debug/reindex-group: group=%s unarchived=%d indexed=%d",
        req.group_id[:20], unarchived, indexed,
    )
    return {"ok": True, "unarchived": unarchived, "reindexed": indexed, "case_ids": case_ids}
