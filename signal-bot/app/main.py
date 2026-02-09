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
)
from app.jobs.types import BUFFER_UPDATE, HISTORY_LINK, MAYBE_RESPOND
from app.jobs.worker import WorkerDeps, worker_loop_forever
from app.llm.client import LLMClient
from app.logging_config import configure_logging
from app.rag.chroma import create_chroma
from app.signal.adapter import NoopSignalAdapter, SignalAdapter
from app.signal.signal_cli import SignalCliAdapter, InboundGroupMessage, InboundDirectMessage


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


def _handle_direct_message(m: InboundDirectMessage) -> None:
    """
    Handle 1:1 messages from admins.
    
    Flow:
    1. Admin adds bot to contacts -> bot sends onboarding prompt
    2. Admin sends group name -> bot finds group, generates QR, sends it
    3. Admin scans QR -> success/fail notification, then loop back to step 2
    """
    admin_id = m.sender
    text = m.text.strip()
    
    log.info("Direct message from %s: %s", admin_id, text[:100])
    
    # Get or create admin session
    session = get_admin_session(db, admin_id)
    
    if session is None:
        # New admin - send onboarding prompt and create session
        log.info("New admin %s, sending onboarding prompt", admin_id)
        set_admin_awaiting_group_name(db, admin_id)
        if isinstance(signal, SignalCliAdapter):
            signal.send_onboarding_prompt(recipient=admin_id)
        return
    
    # Admin is in awaiting_group_name or awaiting_qr_scan state
    # In both cases, if they send text, treat it as a new group name request
    
    if not text:
        # Empty message - resend prompt
        if isinstance(signal, SignalCliAdapter):
            signal.send_onboarding_prompt(recipient=admin_id)
        return
    
    # Try to find group by name
    if not isinstance(signal, SignalCliAdapter):
        log.warning("Signal adapter not available for group lookup")
        return
    
    group = signal.find_group_by_name(text)
    
    if group is None:
        log.info("Group not found for name: %s", text)
        signal.send_group_not_found(recipient=admin_id)
        return
    
    log.info("Found group: %s (%s)", group.group_name, group.group_id)
    
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
    
    # Enqueue job to generate QR code
    enqueue_job(db, HISTORY_LINK, {
        "token": token,
        "admin_id": admin_id,
        "group_id": group.group_id,
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
    
    if isinstance(signal, SignalCliAdapter):
        signal.send_qr_for_group(
            recipient=req.admin_id,
            group_name=req.group_name,
            qr_path=req.qr_path,
        )
    
    return {"ok": True}


class HistoryLinkResultRequest(BaseModel):
    """Called by signal-ingest when QR scan completes or fails."""
    token: str
    success: bool


@app.post("/history/link-result")
def history_link_result(req: HistoryLinkResultRequest) -> dict:
    """
    Callback from signal-ingest after QR code is scanned.
    Notifies the admin of success/failure and resets their state.
    """
    session = get_admin_by_token(db, req.token)
    if session is None:
        log.warning("No admin session found for token %s", req.token)
        return {"ok": False, "error": "session_not_found"}
    
    admin_id = session.admin_id
    group_name = session.pending_group_name or "Unknown"
    group_id = session.pending_group_id
    
    if isinstance(signal, SignalCliAdapter):
        if req.success:
            signal.send_success_message(recipient=admin_id, group_name=group_name)
            if group_id:
                link_admin_to_group(db, admin_id=admin_id, group_id=group_id)
        else:
            signal.send_failure_message(recipient=admin_id, group_name=group_name)
    
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
