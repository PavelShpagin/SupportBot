from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
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
from app.ingestion import hash_sender


settings = load_settings()
configure_logging(settings.log_level)
log = logging.getLogger(__name__)


db = create_db(settings)
ensure_schema(db)

rag = create_chroma(settings)
llm = LLMClient(settings)

signal: SignalAdapter
if settings.signal_listener_enabled:
    signal = SignalCliAdapter(settings)
    signal.assert_available()
else:
    signal = NoopSignalAdapter()

deps = WorkerDeps(settings=settings, db=db, llm=llm, rag=rag, signal=signal)


app = FastAPI()


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


def _handle_direct_message(m: InboundDirectMessage) -> None:
    """
    Handle 1:1 messages from admins.
    
    Flow:
    1. Admin sends first message -> bot sends onboarding prompt (no search)
    2. Admin sends group name -> bot finds group, generates QR, sends it
    3. Admin scans QR -> success/fail notification, then loop back to step 2
    4. If admin sends new message mid-process -> abort current, restart with new group
    
    Language commands: /uk, /en
    """
    from app.db.queries_mysql import set_admin_lang, cancel_pending_history_jobs
    
    admin_id = m.sender
    text = m.text.strip()
    
    log.info("Direct message from %s: %s", admin_id, text[:100])
    
    # Get or create admin session
    session = get_admin_session(db, admin_id)
    
    if session is None:
        # Brand new admin - detect language, send onboarding, DON'T search yet
        detected_lang = _detect_language(text)
        log.info("New admin %s, detected language: %s, sending onboarding only", admin_id, detected_lang)
        set_admin_awaiting_group_name(db, admin_id)
        set_admin_lang(db, admin_id, detected_lang)
        if isinstance(signal, SignalCliAdapter):
            signal.send_onboarding_prompt(recipient=admin_id, lang=detected_lang)
        # Don't search - wait for next message
        return
    
    lang = session.lang
    
    # Handle language commands
    if text.lower() in ("/uk", "/ua"):
        set_admin_lang(db, admin_id, "uk")
        if isinstance(signal, SignalCliAdapter):
            signal.send_lang_changed(recipient=admin_id, lang="uk")
        return
    
    if text.lower() == "/en":
        set_admin_lang(db, admin_id, "en")
        if isinstance(signal, SignalCliAdapter):
            signal.send_lang_changed(recipient=admin_id, lang="en")
        return
    
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


@app.on_event("startup")
def _startup() -> None:
    t = threading.Thread(target=worker_loop_forever, args=(deps,), daemon=True)
    t.start()

    if settings.signal_listener_enabled and isinstance(signal, SignalCliAdapter):
        threading.Thread(
            target=signal.listen_forever,
            kwargs={
                "on_group_message": _handle_group_message,
                "on_direct_message": _handle_direct_message,
                "on_reaction": _handle_reaction,
            },
            daemon=True,
        ).start()

    log.info("Startup complete")


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
    
    if isinstance(signal, SignalCliAdapter):
        signal.send_message(recipient=admin_id, text=progress_text)
    
    return {"ok": True}


class HistoryLinkResultRequest(BaseModel):
    """Called by signal-ingest when processing completes or fails."""
    token: str
    success: bool


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
        rag.upsert_case(
            case_id=case_id,
            document=doc_text,
            embedding=embedding,
            metadata={
                "group_id": req.group_id,
                "status": case.status,
                "evidence_ids": case.evidence_ids,
                "evidence_image_paths": evidence_image_paths,
            },
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
