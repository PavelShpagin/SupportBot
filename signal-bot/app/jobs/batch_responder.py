"""Batch responder: processes unprocessed messages as a batch.

Instead of per-message MAYBE_RESPOND jobs, this module:
1. Takes all unprocessed messages for a group
2. Calls the batch gate to extract questions that need answers
3. For each question, calls the synthesizer with full context
4. Returns all responses (or sends them in production mode)

The batch gate sees ALL unprocessed messages at once, so it naturally
handles consecutive messages from the same user, human-answered questions,
and avoids redundancy by design.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

log = logging.getLogger(__name__)


@dataclass
class BatchResponse:
    """A single response the bot would send."""
    question: str
    message_ids: list[str]
    reply_to_message_id: str
    response_text: str
    attachment_urls: list[str] = field(default_factory=list)
    gate_tag: str = ""


@dataclass
class BatchResult:
    """Result of batch processing a group's unprocessed messages."""
    group_id: str
    unprocessed_count: int
    questions_extracted: int
    responses: list[BatchResponse] = field(default_factory=list)
    skipped_questions: list[dict] = field(default_factory=list)
    gate_raw: dict | None = None


def process_batch(
    *,
    group_id: str,
    db: Any,
    llm: Any,
    ultimate_agent: Any,
    settings: Any,
    bot_sender_hash: str = "",
    last_n: int = 20,
    lang: str = "uk",
    cancel_check: Any = None,
) -> BatchResult:
    """Process unprocessed messages for a group as a batch.

    Args:
        group_id: The group to process.
        db: MySQL database connection.
        llm: LLMClient instance.
        ultimate_agent: UltimateAgent instance.
        settings: App settings.
        bot_sender_hash: Hash of the bot's sender identity.
        last_n: Number of recent messages to treat as "unprocessed".
        lang: Language for responses.
        cancel_check: Optional callable that returns True if processing should abort.

    Returns:
        BatchResult with extracted questions and generated responses.
    """
    from app.db import get_last_messages_text, get_raw_message
    from app.db.queries_mysql import get_last_messages_meta
    from app.jobs.worker import _load_images, _is_image_path

    # Load unprocessed messages (last_n, oldest-first)
    unprocessed_msgs = get_last_messages_meta(db, group_id, n=last_n, bot_sender_hash=bot_sender_hash)

    # Load context before the unprocessed window
    all_db_context = get_last_messages_text(db, group_id, n=settings.context_last_n, bot_sender_hash=bot_sender_hash)

    # Find where unprocessed window starts in DB context
    first_text = ""
    for mm in unprocessed_msgs:
        if (mm.get("content_text") or "").strip():
            first_text = mm["content_text"]
            break

    pre_window_context: list[str] = []
    if first_text:
        for ci, cl in enumerate(all_db_context):
            if first_text[:50] in cl:
                pre_window_context = all_db_context[:ci]
                break
        else:
            pre_window_context = list(all_db_context)
    else:
        pre_window_context = list(all_db_context)

    context_text = "\n".join(pre_window_context)

    # Format unprocessed messages for batch gate
    unprocessed_lines: list[str] = []
    msg_map: dict[str, dict] = {}  # message_id -> meta
    for mm in unprocessed_msgs:
        mid = mm["message_id"]
        sender = mm["sender_hash"]
        text = mm.get("content_text") or ""
        is_bot = mm.get("is_bot", False)
        has_img = False

        # Check if message has images
        msg_obj = get_raw_message(db, message_id=mid)
        if msg_obj and msg_obj.image_paths:
            img_paths = [p for p in msg_obj.image_paths if _is_image_path(p)]
            has_img = bool(img_paths)

        label = "[BOT]" if is_bot else f"User{(sender or 'unknown')[:6]}"
        img_marker = " [IMG]" if has_img else ""
        line = f"[msg_id={mid}] [{label}]: {text}{img_marker}"
        unprocessed_lines.append(line)
        msg_map[mid] = {**mm, "has_images": has_img, "raw_message": msg_obj}

    unprocessed_text = "\n".join(unprocessed_lines)

    log.info("BatchResponder: group=%s unprocessed=%d context_lines=%d",
             group_id[:20], len(unprocessed_msgs), len(pre_window_context))

    # Check cancellation
    if cancel_check and cancel_check():
        log.info("BatchResponder: cancelled before gate")
        return BatchResult(group_id=group_id, unprocessed_count=len(unprocessed_msgs), questions_extracted=0)

    # Call batch gate
    gate_result = llm.batch_gate(
        unprocessed=unprocessed_text,
        context=context_text,
    )

    questions = gate_result.questions
    log.info("BatchResponder: gate extracted %d questions", len(questions))

    result = BatchResult(
        group_id=group_id,
        unprocessed_count=len(unprocessed_msgs),
        questions_extracted=len(questions),
        gate_raw={"questions": [q.model_dump() for q in questions]},
    )

    # Process each question through the synthesizer
    for qi, q in enumerate(questions):
        if cancel_check and cancel_check():
            log.info("BatchResponder: cancelled during synthesis (question %d/%d)", qi + 1, len(questions))
            break

        if qi > 0:
            time.sleep(0.1)  # rate-limit API calls

        # Build context for this question: pre-window + all unprocessed up to this question's messages
        # This way the synthesizer sees the full conversation flow
        question_context_lines = list(pre_window_context)
        for mm in unprocessed_msgs:
            mid = mm["message_id"]
            is_bot = mm.get("is_bot", False)
            text = mm.get("content_text") or ""
            sender = mm["sender_hash"]
            label = "[BOT]" if is_bot else f"User{(sender or 'unknown')[:6]}"
            question_context_lines.append(f"[{label}]: {text}")
            # Stop after we've included all messages that form this question
            if mid in q.message_ids:
                # Include all message_ids, then stop at the last one
                if mid == q.message_ids[-1]:
                    break

        question_context = "\n".join(question_context_lines)

        # Load images from the question's messages
        question_images: list[tuple[bytes, str]] | None = None
        question_text = q.question
        if q.has_images:
            all_loaded: list[tuple[bytes, str]] = []
            for mid in q.message_ids:
                meta = msg_map.get(mid)
                if not meta or not meta.get("has_images"):
                    continue
                msg_obj = meta.get("raw_message")
                if msg_obj and msg_obj.image_paths:
                    loaded = _load_images(
                        settings=settings,
                        image_paths=[p for p in msg_obj.image_paths if _is_image_path(p)],
                        max_images=2,
                        total_budget_bytes=settings.max_total_image_bytes,
                    )
                    all_loaded.extend(loaded)
            if all_loaded:
                question_images = all_loaded[:4]  # cap at 4 images
                markers = " ".join(f"[[IMG:{j}]]" for j in range(len(question_images)))
                question_text = f"{q.question}\n{markers}"

        # Call synthesizer
        try:
            raw_answer = ultimate_agent.answer(
                question_text,
                group_id=group_id,
                db=db,
                lang=lang,
                context=question_context,
                images=question_images,
                gate_tag="batch_question",
            )
            resp_text = raw_answer.text if hasattr(raw_answer, 'text') else str(raw_answer)

            if resp_text and resp_text.strip() and resp_text != "SKIP":
                result.responses.append(BatchResponse(
                    question=q.question,
                    message_ids=q.message_ids,
                    reply_to_message_id=q.reply_to_message_id,
                    response_text=resp_text,
                    attachment_urls=raw_answer.attachment_urls if hasattr(raw_answer, 'attachment_urls') else [],
                ))
            else:
                result.skipped_questions.append({
                    "question": q.question,
                    "message_ids": q.message_ids,
                    "reason": "synthesizer_skip",
                })
        except Exception as exc:
            log.warning("BatchResponder: synthesizer failed for question %d: %s", qi, exc)
            result.skipped_questions.append({
                "question": q.question,
                "message_ids": q.message_ids,
                "reason": f"error: {exc}",
            })

    log.info("BatchResponder: group=%s responses=%d skipped=%d",
             group_id[:20], len(result.responses), len(result.skipped_questions))
    return result
