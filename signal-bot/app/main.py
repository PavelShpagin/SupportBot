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
)
from app.jobs.types import BUFFER_UPDATE, HISTORY_LINK, MAYBE_RESPOND
from app.jobs.worker import WorkerDeps, worker_loop_forever
from app.llm.client import LLMClient
from app.logging_config import configure_logging
from app.rag.chroma import create_chroma
from app.signal.adapter import NoopSignalAdapter, SignalAdapter
from app.signal.signal_cli import InboundSignalMessage, SignalCliAdapter


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


@app.on_event("startup")
def _startup() -> None:
    t = threading.Thread(target=worker_loop_forever, args=(deps,), daemon=True)
    t.start()

    if settings.signal_listener_enabled and isinstance(signal, SignalCliAdapter):
        def _on_message(m: InboundSignalMessage) -> None:
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

        threading.Thread(
            target=signal.listen_forever,
            kwargs={"on_message": _on_message},
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
    token = uuid.uuid4().hex
    db_create_history_token(db, token=token, admin_id=req.admin_id, group_id=req.group_id, ttl_minutes=settings.history_token_ttl_minutes)
    # Ask signal-ingest to generate a QR for linking (writes /var/lib/history/{token}.png).
    enqueue_job(db, HISTORY_LINK, {"token": token, "admin_id": req.admin_id, "group_id": req.group_id})
    return HistoryTokenResponse(token=token)


@app.get("/history/qr/{token}")
def history_qr(token: str) -> FileResponse:
    p = Path("/var/lib/history") / f"{token}.png"
    if not p.exists():
        raise HTTPException(status_code=404, detail="QR not ready (signal-ingest has not produced it yet)")
    return FileResponse(path=str(p), media_type="image/png")


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
            metadata={"group_id": req.group_id, "status": case.status, "evidence_ids": case.evidence_ids},
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
    # This endpoint exists so the pipeline can be tested without Signal I/O.
    # Protect it at the network layer (or remove it) in production deployments.
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

