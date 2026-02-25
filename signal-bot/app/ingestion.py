from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Iterable, Optional

from app.config import Settings
from app.db import insert_raw_message, enqueue_job, RawMessage
from app.jobs.types import BUFFER_UPDATE, MAYBE_RESPOND
from app.llm.client import LLMClient

log = logging.getLogger(__name__)


def _sender_hash(sender: str) -> str:
    return hashlib.sha256(sender.encode("utf-8")).hexdigest()[:16]


# Expose for use in main.py reaction handler
hash_sender = _sender_hash


def ingest_message(
    *,
    settings: Settings,
    db,  # Database (MySQL or Oracle)
    llm: LLMClient,
    message_id: str,
    group_id: str,
    sender: str,
    ts: int,
    text: str,
    image_paths: Iterable[str] = (),
    reply_to_id: str | None = None,
) -> None:
    content_text = text or ""
    context_text = text or ""
    stored_image_paths: list[str] = []

    for p in image_paths:
        img_path = Path(p)
        if not img_path.is_absolute():
            # signal-cli often reports stored filenames relative to the config dir.
            img_path = Path(settings.signal_bot_storage) / img_path
        try:
            img_path = img_path.resolve()
        except Exception:
            img_path = img_path.absolute()
        if not img_path.exists():
            log.warning("Attachment missing on disk: %s", img_path)
            continue
        stored_image_paths.append(str(img_path))
        image_bytes = img_path.read_bytes()
        try:
            j = llm.image_to_text_json(image_bytes=image_bytes, context_text=context_text)
            # Store the OCR text without appending the raw JSON to the message content visible to users
            # The RAG pipeline will still embed this content_text. But we should make it readable.
            extracted_text = j.extracted_text or ""
            observations = ", ".join(j.observations) if j.observations else ""
            
            ocr_summary = []
            if extracted_text:
                ocr_summary.append(f"Текст на зображенні: {extracted_text}")
            if observations:
                ocr_summary.append(f"Елементи на зображенні: {observations}")
                
            if ocr_summary:
                content_text = content_text + "\n\n[Зображення: " + " | ".join(ocr_summary) + "]"
        except Exception:
            log.exception("Image extraction failed (path=%s).", img_path)
            content_text = content_text + "\n\n[Зображення]"

    inserted = insert_raw_message(
        db,
        RawMessage(
            message_id=message_id,
            group_id=group_id,
            ts=ts,
            sender_hash=_sender_hash(sender),
            content_text=content_text,
            image_paths=stored_image_paths,
            reply_to_id=reply_to_id,
        ),
    )
    
    if not inserted:
        log.info("Message %s already exists, skipping response generation", message_id)
        return

    # Include original sender/text in job payload so the responder can:
    # - reply/quote the exact asker (Signal "quote" feature)
    # - keep user-facing quotes free of internal [image] JSON expansions
    job_payload = {
        "group_id": group_id,
        "message_id": message_id,
        "sender": sender,
        "ts": ts,
        "text": text or "",
    }
    # MAYBE_RESPOND must be enqueued first so it runs before BUFFER_UPDATE.
    # When the bot finds a RAG answer it marks the message as rag-answered;
    # BUFFER_UPDATE then skips B1 case creation for that message.
    enqueue_job(db, MAYBE_RESPOND, job_payload)
    enqueue_job(db, BUFFER_UPDATE, job_payload)

