from __future__ import annotations

import hashlib
import logging
import mimetypes
from pathlib import Path
from typing import Iterable

import app.r2 as _r2
from app.config import Settings
from app.db import insert_raw_message, enqueue_job, RawMessage
from app.jobs.types import BUFFER_UPDATE, MAYBE_RESPOND
from app.llm.client import LLMClient

log = logging.getLogger(__name__)


def _is_video(content_type: str) -> bool:
    return content_type.startswith("video/")


def _extract_video_thumbnail(video_path: str | Path) -> bytes | None:
    """Extract a single thumbnail frame from a video file using OpenCV.

    Picks a frame at ~1 second (or the first frame for very short videos).
    Returns JPEG bytes or None on failure.
    """
    try:
        import cv2

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return None

        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        target_frame = min(int(fps), max(total - 1, 0))
        cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)

        ret, frame = cap.read()
        cap.release()
        if not ret or frame is None:
            return None

        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return buf.tobytes() if ok else None
    except Exception as exc:
        log.debug("Video thumbnail extraction failed for %s: %s", video_path, exc)
        return None


def _extract_video_thumbnail_from_bytes(data: bytes) -> bytes | None:
    """Extract a thumbnail from in-memory video bytes via a temp file."""
    import tempfile
    import os
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        result = _extract_video_thumbnail(tmp_path)
        return result
    except Exception as exc:
        log.debug("Video thumbnail extraction from bytes failed: %s", exc)
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def _guess_mime(path: str) -> str:
    """Accept-all MIME guessing: stdlib first, fallback to application/octet-stream."""
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"


def _is_image(content_type: str) -> bool:
    return content_type.startswith("image/")


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
            img_path = Path(settings.signal_bot_storage) / img_path
        try:
            img_path = img_path.resolve()
        except Exception:
            img_path = img_path.absolute()
        if not img_path.exists():
            log.warning("Attachment missing on disk: %s", img_path)
            continue
        file_bytes = img_path.read_bytes()

        ct = _guess_mime(str(img_path))

        stored_path = str(img_path)
        if _r2.is_enabled():
            r2_key = f"attachments/{group_id}/{img_path.name}"
            r2_url = _r2.upload(r2_key, file_bytes, ct)
            if r2_url:
                stored_path = r2_url
        stored_image_paths.append(stored_path)

        if _is_image(ct):
            try:
                j = llm.image_to_text_json(image_bytes=file_bytes, context_text=context_text)
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
        elif _is_video(ct):
            thumb_bytes = _extract_video_thumbnail(img_path)
            if thumb_bytes:
                thumb_name = img_path.stem + "_thumb.jpg"
                thumb_stored = None
                if _r2.is_enabled():
                    thumb_key = f"attachments/{group_id}/{thumb_name}"
                    thumb_stored = _r2.upload(thumb_key, thumb_bytes, "image/jpeg")
                if thumb_stored:
                    stored_image_paths.append(thumb_stored)
                try:
                    j = llm.image_to_text_json(image_bytes=thumb_bytes, context_text=f"Video thumbnail from: {img_path.name}\n{context_text}")
                    extracted_text = j.extracted_text or ""
                    observations = ", ".join(j.observations) if j.observations else ""
                    summary_parts = []
                    if extracted_text:
                        summary_parts.append(f"Текст: {extracted_text}")
                    if observations:
                        summary_parts.append(f"Елементи: {observations}")
                    desc = " | ".join(summary_parts) if summary_parts else ""
                    content_text = content_text + f"\n\n[Відео: {img_path.name}" + (f" — {desc}" if desc else "") + "]"
                except Exception:
                    log.debug("Video thumbnail OCR failed for %s", img_path)
                    content_text = content_text + f"\n\n[Відео: {img_path.name}]"
            else:
                content_text = content_text + f"\n\n[Відео: {img_path.name}]"
        else:
            fname = img_path.name
            content_text = content_text + f"\n\n[attachment: {fname} ({ct})]"

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

    job_payload = {
        "group_id": group_id,
        "message_id": message_id,
        "sender": sender,
        "ts": ts,
        "text": text or "",
    }
    enqueue_job(db, MAYBE_RESPOND, job_payload)
    enqueue_job(db, BUFFER_UPDATE, job_payload)

