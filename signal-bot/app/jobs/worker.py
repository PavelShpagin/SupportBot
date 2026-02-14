from __future__ import annotations

import json
import logging
import mimetypes
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, TYPE_CHECKING

from app.config import Settings
from app.db import (
    RawMessage,
    claim_next_job,
    complete_job,
    fail_job,
    get_raw_message,
    get_buffer,
    set_buffer,
    new_case_id,
    insert_case,
    get_last_messages_text,
    get_positive_reactions_for_message,
    get_message_by_ts,
)
from app.jobs import types as job_types
from app.llm.client import LLMClient
from app.rag.chroma import ChromaRag
from app.signal.adapter import SignalAdapter

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerDeps:
    settings: Settings
    db: Any  # Database (MySQL or Oracle)
    llm: LLMClient
    rag: ChromaRag
    signal: SignalAdapter


def _format_buffer_line(msg: RawMessage, positive_reactions: int = 0) -> str:
    reply = f" reply_to={msg.reply_to_id}" if msg.reply_to_id else ""
    reactions = f" reactions={positive_reactions}" if positive_reactions > 0 else ""
    return f"{msg.sender_hash} ts={msg.ts}{reply}{reactions}\n{msg.content_text}\n\n"


@dataclass(frozen=True)
class BufferMessageBlock:
    idx: int
    start_line: int
    end_line: int
    raw_text: str


_BUFFER_HEADER_RE = re.compile(r"^[^\n]*\sts=\d+(?:\sreply_to=\S+)?\n", re.MULTILINE)


def _parse_buffer_blocks(buffer_text: str) -> List[BufferMessageBlock]:
    """Parse buffer into exact message blocks with stable 0-based indexes."""
    if not buffer_text:
        return []

    headers = list(_BUFFER_HEADER_RE.finditer(buffer_text))
    if not headers:
        return []

    blocks: List[BufferMessageBlock] = []
    for i, m in enumerate(headers):
        start = m.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(buffer_text)
        raw = buffer_text[start:end]
        start_line = buffer_text.count("\n", 0, start) + 1
        end_line = start_line + raw.count("\n")
        blocks.append(
            BufferMessageBlock(
                idx=i,
                start_line=start_line,
                end_line=end_line,
                raw_text=raw,
            )
        )
    return blocks


def _format_numbered_buffer_for_extract(blocks: List[BufferMessageBlock]) -> str:
    """Build numbered extract input so LLM can return exact idx/line spans."""
    out: List[str] = []
    for b in blocks:
        out.append(f"### MSG idx={b.idx} lines={b.start_line}-{b.end_line}")
        out.append(b.raw_text.rstrip("\n"))
        out.append("### END")
        out.append("")
    return "\n".join(out).strip()


def _trim_buffer(buffer_text: str, max_age_hours: int, max_messages: int) -> str:
    """Trim buffer to enforce age and size limits.
    
    Removes oldest messages first until within limits.
    """
    import time as _time
    
    if not buffer_text:
        return ""
    
    # Parse buffer into message blocks with timestamps
    blocks = buffer_text.split("\n\n")
    parsed_blocks: List[tuple] = []  # (timestamp, block_text)
    
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        
        # Extract timestamp from first line
        first_line = block.split("\n")[0] if block else ""
        ts_value = 0
        if " ts=" in first_line:
            ts_start = first_line.find(" ts=") + 4
            ts_end = first_line.find(" ", ts_start)
            if ts_end == -1:
                ts_end = len(first_line)
            try:
                ts_value = int(first_line[ts_start:ts_end])
            except ValueError:
                pass
        
        parsed_blocks.append((ts_value, block))
    
    if not parsed_blocks:
        return ""
    
    # Sort by timestamp (oldest first)
    parsed_blocks.sort(key=lambda x: x[0])
    
    # Apply age limit (remove messages older than max_age_hours)
    current_ts = int(_time.time() * 1000)  # Current time in milliseconds
    max_age_ms = max_age_hours * 3600 * 1000
    cutoff_ts = current_ts - max_age_ms
    
    filtered_blocks = [(ts, block) for ts, block in parsed_blocks if ts >= cutoff_ts or ts == 0]
    
    # Apply message count limit (keep most recent)
    if len(filtered_blocks) > max_messages:
        filtered_blocks = filtered_blocks[-max_messages:]
    
    # Reconstruct buffer
    if not filtered_blocks:
        return ""
    return "\n\n".join(block for _, block in filtered_blocks) + "\n\n"


def _mentions_bot(settings: Settings, text: str) -> bool:
    low = text.lower()
    return any(m.lower() in low for m in settings.bot_mention_strings)


def _guess_mime(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    return mime or "image/png"


def _load_images(
    *,
    settings: Settings,
    image_paths: List[str],
    max_images: int,
    total_budget_bytes: int,
) -> List[tuple[bytes, str]]:
    if max_images <= 0 or total_budget_bytes <= 0:
        return []

    images: List[tuple[bytes, str]] = []
    total = 0
    for p in image_paths:
        if len(images) >= max_images:
            break
        try:
            if not p:
                continue
            img_path = p
            mime = _guess_mime(img_path)
            with open(img_path, "rb") as f:
                data = f.read()
            size = len(data)
        except Exception:
            log.warning("Failed to read image for multimodal call: %s", p)
            continue

        if size > settings.max_image_size_bytes:
            log.warning("Skipping large image (%s bytes): %s", size, p)
            continue
        if total + size > total_budget_bytes:
            break
        images.append((data, mime))
        total += size
    return images


def _collect_evidence_image_paths(deps: WorkerDeps, evidence_ids: List[str]) -> List[str]:
    paths: List[str] = []
    for mid in evidence_ids:
        msg = get_raw_message(deps.db, message_id=mid)
        if msg is None:
            continue
        for p in msg.image_paths:
            if p:
                paths.append(p)
    return paths


def _split_case_document(doc: str) -> tuple[str, str, str]:
    """
    Split the stored case document into (title, problem_summary, solution_summary).

    Production doc_text format (see worker buffer handler) is:
      title\nproblem_summary\nsolution_summary\n(tags: ...)

    The solution summary may contain internal newlines; we join them into one line for quoting.
    """
    lines = [ln.strip() for ln in (doc or "").splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return "", "", ""

    tags_idx = len(lines)
    for i, ln in enumerate(lines):
        if ln.lower().startswith("tags:"):
            tags_idx = i
            break

    title = lines[0] if len(lines) >= 1 else ""
    problem = lines[1] if len(lines) >= 2 else ""
    solution_lines = lines[2:tags_idx] if len(lines) >= 3 else []
    solution = " ".join(solution_lines).strip()
    return title, problem, solution


def _get_solution_message_for_reply(
    db, case: Dict[str, Any]
) -> tuple[str | None, int | None, str | None, str | None]:
    """
    Get the last message from evidence_ids to reply to (the solution message).
    Returns (message_id, timestamp, text, author) or (None, None, None, None).
    """
    evidence_ids = case.get("metadata", {}).get("evidence_ids") or []
    if not evidence_ids:
        return None, None, None, None
    
    # Get the last evidence message (typically contains the solution)
    last_msg_id = evidence_ids[-1] if isinstance(evidence_ids, list) else None
    if not last_msg_id:
        return None, None, None, None
    
    msg = get_raw_message(db, message_id=str(last_msg_id))
    if not msg:
        return None, None, None, None
    
    return str(msg.message_id), int(msg.ts), str(msg.content_text or ""), str(msg.sender)


def _pick_history_solution_refs(retrieved: List[Dict[str, Any]], *, max_refs: int) -> List[Dict[str, str]]:
    """
    Pick 1..N solved cases that contain a non-empty solution summary.

    Returns items like: {"case_id": "...", "title": "...", "solution": "..."}.
    """
    out: List[Dict[str, str]] = []
    for item in retrieved:
        meta = item.get("metadata") if isinstance(item, dict) else {}
        status = (meta or {}).get("status")
        if status != "solved":
            continue
        cid = str(item.get("case_id") or "").strip()
        if not cid:
            continue
        title, _problem, solution = _split_case_document(str(item.get("document") or ""))
        if not solution.strip():
            continue
        out.append(
            {
                "case_id": cid,
                "title": title.strip(),
                "solution": solution.strip(),
            }
        )
        if len(out) >= max_refs:
            break
    return out


def _append_history_block(text: str, refs: List[Dict[str, str]]) -> str:
    if not refs:
        return text
    lines: List[str] = [text.rstrip(), "", "Історія (використано вирішені кейси):"]
    for r in refs:
        # Keep this user-facing: include the concrete solution summary text.
        sol = r["solution"]
        if len(sol) > 320:
            sol = sol[:317].rstrip() + "..."
        title = r.get("title") or ""
        if title:
            lines.append(f'- case:{r["case_id"]} — {title}: {sol}')
        else:
            lines.append(f'- case:{r["case_id"]}: {sol}')
    return "\n".join(lines).strip() + "\n"


def worker_loop_forever(deps: WorkerDeps) -> None:
    log.info("Worker loop started")
    while True:
        job = claim_next_job(
            deps.db,
            allowed_types=[job_types.BUFFER_UPDATE, job_types.MAYBE_RESPOND],
        )
        if job is None:
            time.sleep(deps.settings.worker_poll_seconds)
            continue

        try:
            if job.type == job_types.BUFFER_UPDATE:
                _handle_buffer_update(deps, job.payload)
            elif job.type == job_types.MAYBE_RESPOND:
                _handle_maybe_respond(deps, job.payload)
            else:
                log.warning("Unknown job type=%s job_id=%s (marking done)", job.type, job.job_id)

            complete_job(deps.db, job_id=job.job_id)
        except Exception:
            log.exception("Job failed: id=%s type=%s", job.job_id, job.type)
            fail_job(deps.db, job_id=job.job_id, attempts=job.attempts)


def _handle_buffer_update(deps: WorkerDeps, payload: Dict[str, Any]) -> None:
    group_id = str(payload["group_id"])
    message_id = str(payload["message_id"])

    msg = get_raw_message(deps.db, message_id=message_id)
    if msg is None:
        log.warning("BUFFER_UPDATE: message not found: %s", message_id)
        return

    # Check for positive reactions on this message
    positive_reactions = get_positive_reactions_for_message(deps.db, group_id=group_id, target_ts=msg.ts)
    line = _format_buffer_line(msg, positive_reactions=positive_reactions)
    buf = get_buffer(deps.db, group_id=group_id)
    buf2 = (buf or "") + line
    
    # Trim buffer to enforce size/age limits before processing
    buf2 = _trim_buffer(
        buf2, 
        max_age_hours=deps.settings.buffer_max_age_hours,
        max_messages=deps.settings.buffer_max_messages
    )

    blocks = _parse_buffer_blocks(buf2)
    if not blocks:
        set_buffer(deps.db, group_id=group_id, buffer_text=buf2)
        return

    numbered_buffer = _format_numbered_buffer_for_extract(blocks)
    extract = deps.llm.extract_case_from_buffer(buffer_text=numbered_buffer)
    if not extract.cases:
        set_buffer(deps.db, group_id=group_id, buffer_text=buf2)
        return

    # Hard safety: if any returned span is out of bounds, reject this extract output.
    n_blocks = len(blocks)
    if any(c.start_idx < 0 or c.end_idx >= n_blocks for c in extract.cases):
        log.warning(
            "Rejecting extract result with out-of-range spans (n_blocks=%s): %s",
            n_blocks,
            [(c.start_idx, c.end_idx) for c in extract.cases],
        )
        set_buffer(deps.db, group_id=group_id, buffer_text=buf2)
        return

    accepted_ranges: List[tuple[int, int]] = []
    for span in extract.cases:
        # Build exact case block from indexed message slice.
        case_block_text = "".join(blocks[i].raw_text for i in range(span.start_idx, span.end_idx + 1))
        case = deps.llm.make_case(case_block_text=case_block_text)
        if not case.keep:
            continue

        if case.status != "solved":
            log.info(
                "Skipping non-solved extracted span idx=%s..%s (status=%s)",
                span.start_idx,
                span.end_idx,
                case.status,
            )
            continue

        if not case.solution_summary.strip():
            log.warning("Rejecting solved case without solution_summary for span %s..%s", span.start_idx, span.end_idx)
            continue

        case_id = new_case_id(deps.db)
        evidence_image_paths = _collect_evidence_image_paths(deps, case.evidence_ids)
        insert_case(
            deps.db,
            case_id=case_id,
            group_id=group_id,
            status=case.status,
            problem_title=case.problem_title,
            problem_summary=case.problem_summary,
            solution_summary=case.solution_summary,
            tags=case.tags,
            evidence_ids=case.evidence_ids,
            evidence_image_paths=evidence_image_paths,
        )

        # Build doc_text with clear labels for retrieval
        doc_text = "\n".join(
            [
                f"[SOLVED] {case.problem_title.strip()}",
                f"Проблема: {case.problem_summary.strip()}",
                f"Рішення: {case.solution_summary.strip()}",
                "tags: " + ", ".join(case.tags),
            ]
        ).strip()
        embedding = deps.llm.embed(text=doc_text)

        deps.rag.upsert_case(
            case_id=case_id,
            document=doc_text,
            embedding=embedding,
            metadata={
                "group_id": group_id,
                "status": case.status,
                "evidence_ids": case.evidence_ids,
                "evidence_image_paths": evidence_image_paths,
            },
        )
        accepted_ranges.append((span.start_idx, span.end_idx))

    if not accepted_ranges:
        set_buffer(deps.db, group_id=group_id, buffer_text=buf2)
        return

    remove_idx: set[int] = set()
    for start_idx, end_idx in accepted_ranges:
        remove_idx.update(range(start_idx, end_idx + 1))
    kept_blocks = [b.raw_text for b in blocks if b.idx not in remove_idx]
    buffer_new = "".join(kept_blocks)

    set_buffer(deps.db, group_id=group_id, buffer_text=buffer_new)
    log.info(
        "Extracted solved spans group_id=%s total_messages=%s removed_ranges=%s removed_messages=%s remaining_messages=%s",
        group_id,
        len(blocks),
        accepted_ranges,
        len(remove_idx),
        len(blocks) - len(remove_idx),
    )


def _handle_maybe_respond(deps: WorkerDeps, payload: Dict[str, Any]) -> None:
    group_id = str(payload["group_id"])
    message_id = str(payload["message_id"])

    msg = get_raw_message(deps.db, message_id=message_id)
    if msg is None:
        log.warning("MAYBE_RESPOND: message not found: %s", message_id)
        return

    # Get CLEAN buffer (only unsolved threads after extraction)
    buffer = get_buffer(deps.db, group_id=group_id) or ""
    
    # Use clean buffer for both gate and response (keep it simple)
    context = buffer

    force = _mentions_bot(deps.settings, msg.content_text)
    msg_images = _load_images(
        settings=deps.settings,
        image_paths=msg.image_paths,
        max_images=deps.settings.max_images_per_gate,
        total_budget_bytes=deps.settings.max_total_image_bytes,
    )
    if not force:
        decision = deps.llm.decide_consider(message=msg.content_text, context=context, images=msg_images)
        if not decision.consider:
            return

    query_text = msg.content_text.strip()
    if not query_text:
        return

    query_embedding = deps.llm.embed(text=query_text)
    retrieved = deps.rag.retrieve_cases(
        group_id=group_id,
        embedding=query_embedding,
        k=deps.settings.retrieve_top_k,
    )

    # Minimal safety: only block if truly nothing available (edge case)
    # Trust the LLM to make the final decision based on case relevance
    if len(retrieved) == 0 and len(buffer.strip()) == 0:
        log.info("No retrieved cases and empty buffer; staying silent")
        return

    kb_paths: List[str] = []
    for item in retrieved:
        paths = item.get("metadata", {}).get("evidence_image_paths") or []
        if not isinstance(paths, list):
            continue
        kb_paths.extend([str(p) for p in paths if str(p)])

    max_kb = deps.settings.max_kb_images_per_case * max(1, len(retrieved))
    kb_paths = kb_paths[:max_kb]

    kb_images = _load_images(
        settings=deps.settings,
        image_paths=kb_paths,
        max_images=deps.settings.max_images_per_respond,
        total_budget_bytes=max(deps.settings.max_total_image_bytes - sum(len(b) for b, _ in msg_images), 0),
    )

    all_images = msg_images + kb_images
    if len(all_images) > deps.settings.max_images_per_respond:
        all_images = all_images[: deps.settings.max_images_per_respond]

    cases_json = json.dumps(retrieved, ensure_ascii=False, indent=2)
    resp = deps.llm.decide_and_respond(
        message=msg.content_text,
        context=context,
        cases=cases_json,
        buffer=buffer,
        images=all_images,
    )
    if not resp.respond:
        return

    # NOW extract history refs for citation (after LLM decided to respond)
    history_refs = _pick_history_solution_refs(retrieved, max_refs=1)
    
    # Ensure at least one case citation is present (best-effort, even if model forgets).
    required_case_cits = [f'case:{r["case_id"]}' for r in history_refs]
    cits = list(dict.fromkeys((resp.citations or []) + required_case_cits))

    out = resp.text.strip()
    # User requested to hide refs and history block for cleaner UI
    # out = _append_history_block(out, history_refs)
    # if cits:
    #     out = out.rstrip() + "\n\nRefs: " + ", ".join(cits[:3]) + "\n"

    # NEW: Dual-reply strategy for trust:
    # 1. Reply to the user's question (quote_author)
    # 2. Also reply to the solution message from the top-1 case (if high confidence)
    
    quote_author = str(payload.get("sender") or "").strip()
    quote_ts_raw = payload.get("ts")
    quote_ts = int(quote_ts_raw) if quote_ts_raw is not None else int(msg.ts)
    quote_msg = str(payload.get("text") or "").strip()
    
    # Check if we have high confidence (top-1 case with good similarity)
    # If so, also reply to the solution message from that case
    solution_msg_id, solution_ts, solution_text, solution_author = None, None, None, None
    if len(retrieved) > 0:
        top_case = retrieved[0]
        # If distance is low (high similarity), reply to solution message too
        distance = top_case.get("distance", 1.0)
        if distance < 0.5:  # High confidence threshold
            solution_msg_id, solution_ts, solution_text, solution_author = _get_solution_message_for_reply(deps.db, top_case)
    
    # For now, prioritize replying to the solution message (more useful for verification)
    # If solution message found, quote it; otherwise quote the question
    final_quote_ts = solution_ts if solution_ts else quote_ts
    final_quote_author = solution_author if solution_ts else quote_author
    final_quote_msg = solution_text[:200] if solution_text else (quote_msg if quote_msg else None)
    
    # Add @ mention for the person asking
    mention_recipients = [quote_author] if quote_author else []
    
    if final_quote_ts and final_quote_author:
        deps.signal.send_group_text(
            group_id=group_id,
            text=out,
            quote_timestamp=final_quote_ts,
            quote_author=final_quote_author,
            quote_message=final_quote_msg,
            mention_recipients=mention_recipients,
        )
    else:
        deps.signal.send_group_text(
            group_id=group_id, 
            text=out,
            mention_recipients=mention_recipients,
        )

