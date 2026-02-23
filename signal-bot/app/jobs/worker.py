from __future__ import annotations

import json
import logging
import mimetypes
import re
import threading
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
    get_open_cases_for_group,
    update_case_to_solved,
    mark_case_in_rag,
    expire_old_open_cases,
    get_all_active_case_ids,
    store_case_embedding,
    find_similar_case,
    merge_case,
    archive_case,
)
from app.jobs import types as job_types
from app.llm.client import LLMClient
from app.rag.chroma import ChromaRag
from app.signal.adapter import SignalAdapter
from app.agent.ultimate_agent import UltimateAgent

log = logging.getLogger(__name__)

# message_id set: messages for which the bot sent a real RAG answer (not a pure
# [[TAG_ADMIN]] escalation).  When BUFFER_UPDATE processes the buffer afterwards,
# it skips these messages so no B1 open case is created for an already-answered
# question.  Uses an ordered dict so we can do FIFO eviction.
_rag_answered_messages: dict[str, None] = {}
_rag_answered_lock = threading.Lock()
_RAG_ANSWERED_MAX = 2000


@dataclass(frozen=True)
class WorkerDeps:
    settings: Settings
    db: Any  # Database (MySQL or Oracle)
    llm: LLMClient
    rag: ChromaRag
    signal: SignalAdapter
    ultimate_agent: UltimateAgent
    bot_sender_hash: str = ""  # hash of the bot's own phone number — used to skip bot messages in extraction


def _format_buffer_line(msg: RawMessage, positive_reactions: int = 0, is_bot: bool = False) -> str:
    reply = f" reply_to={msg.reply_to_id}" if msg.reply_to_id else ""
    reactions = f" reactions={positive_reactions}" if positive_reactions > 0 else ""
    bot_tag = " [BOT]" if is_bot else ""
    # Include message_id so LLM can extract evidence_ids for case linking
    return f"{msg.sender_hash}{bot_tag} ts={msg.ts} msg_id={msg.message_id}{reply}{reactions}\n{msg.content_text}\n\n"


@dataclass(frozen=True)
class BufferMessageBlock:
    idx: int
    start_line: int
    end_line: int
    raw_text: str
    message_id: str  # Extracted from msg_id= in header


# Updated regex to match new format with msg_id
_BUFFER_HEADER_RE = re.compile(r"^[^\n]*\sts=\d+(?:\s+msg_id=\S+)?(?:\s+reply_to=\S+)?(?:\s+reactions=\d+)?\n", re.MULTILINE)
# Regex to extract msg_id from header line
_MSG_ID_RE = re.compile(r"msg_id=(\S+)")


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
        
        # Extract message_id from header
        header_line = raw.split("\n")[0] if raw else ""
        msg_id_match = _MSG_ID_RE.search(header_line)
        message_id = msg_id_match.group(1) if msg_id_match else ""
        
        blocks.append(
            BufferMessageBlock(
                idx=i,
                start_line=start_line,
                end_line=end_line,
                raw_text=raw,
                message_id=message_id,
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


_EXPIRY_INTERVAL_SECONDS = 3600   # run B1 expiry check once per hour
_SYNC_RAG_INTERVAL_SECONDS = 3600  # reconcile Chroma vs MySQL once per hour


def _run_sync_rag(deps: WorkerDeps) -> None:
    """Remove ChromaDB entries whose case_id no longer exists in MySQL as active.

    This is the authoritative reconciliation that replaces the per-query
    _case_exists_in_db() check. Running it periodically means Chroma stays
    clean even if an individual upsert/delete failed mid-flight.
    """
    try:
        active_ids = set(get_all_active_case_ids(deps.db))
    except Exception:
        log.exception("SYNC_RAG: failed to load active case IDs from MySQL")
        return

    try:
        chroma_ids = set(deps.rag.list_all_case_ids())
    except Exception:
        log.exception("SYNC_RAG: failed to list ChromaDB case IDs")
        return

    stale = chroma_ids - active_ids
    if not stale:
        log.debug("SYNC_RAG: Chroma and MySQL are in sync (%d active cases)", len(active_ids))
        return

    log.info("SYNC_RAG: removing %d stale Chroma entries: %s", len(stale), list(stale)[:10])
    try:
        deps.rag.delete_cases(list(stale))
    except Exception:
        log.exception("SYNC_RAG: failed to delete stale Chroma entries")


def worker_loop_forever(deps: WorkerDeps) -> None:
    log.info("Worker loop started")
    last_expiry_check = 0.0
    last_sync_rag = 0.0

    while True:
        now = time.time()

        # Periodic B1 expiry: delete open cases older than 7 days
        if now - last_expiry_check >= _EXPIRY_INTERVAL_SECONDS:
            try:
                expired = expire_old_open_cases(deps.db, max_age_days=7)
                if expired:
                    log.info("Expired %d stale B1 open cases: %s", len(expired), expired)
            except Exception:
                log.exception("B1 expiry cleanup failed")
            last_expiry_check = now

        # Periodic RAG sync: remove Chroma entries with no matching MySQL case
        if now - last_sync_rag >= _SYNC_RAG_INTERVAL_SECONDS:
            _run_sync_rag(deps)
            last_sync_rag = now

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
    is_bot_msg = bool(deps.bot_sender_hash and msg.sender_hash == deps.bot_sender_hash)
    line = _format_buffer_line(msg, positive_reactions=positive_reactions, is_bot=is_bot_msg)
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

    # Build extraction view: exclude bot messages AND messages the bot already
    # answered via RAG — both are kept in buf2 for context but must not generate
    # new open B1 cases.
    with _rag_answered_lock:
        local_rag_answered = set(_rag_answered_messages.keys())
    non_bot_blocks = [
        b for b in blocks
        if "[BOT]" not in b.raw_text.splitlines()[0]
        and b.message_id not in local_rag_answered
    ]
    if not non_bot_blocks:
        set_buffer(deps.db, group_id=group_id, buffer_text=buf2)
        return

    numbered_buffer = _format_numbered_buffer_for_extract(non_bot_blocks)
    extract = deps.llm.extract_case_from_buffer(buffer_text=numbered_buffer)
    if not extract.cases:
        set_buffer(deps.db, group_id=group_id, buffer_text=buf2)
        return

    # Hard safety: if any returned span is out of bounds, reject this extract output.
    n_blocks = len(non_bot_blocks)
    if any(c.start_idx < 0 or c.end_idx >= n_blocks for c in extract.cases):
        log.warning(
            "Rejecting extract result with out-of-range spans (n_blocks=%s): %s",
            n_blocks,
            [(c.start_idx, c.end_idx) for c in extract.cases],
        )
        set_buffer(deps.db, group_id=group_id, buffer_text=buf2)
        return

    # ── Phase 1: Extract new cases from the current buffer spans ────────────
    accepted_ranges: List[tuple[int, int]] = []  # solved ranges (indices in non_bot_blocks) to remove
    for span in extract.cases:
        # Build exact case block from non-bot indexed message slice.
        case_block_text = "".join(non_bot_blocks[i].raw_text for i in range(span.start_idx, span.end_idx + 1))
        case = deps.llm.make_case(case_block_text=case_block_text)
        if not case.keep:
            continue

        # Require at least one positive emoji reaction in the span before accepting
        # the LLM's "solved" verdict for live cases. Without emoji, the "solution"
        # might just be the user's own continued narrative (no human confirmation).
        span_has_reaction = bool(re.search(r'\breactions=[1-9]', case_block_text))
        effective_status = case.status if span_has_reaction else "open"
        if case.status == "solved" and not span_has_reaction:
            log.info(
                "Case span %s..%s: LLM said solved but no emoji reaction → stored as open (pending confirmation)",
                span.start_idx, span.end_idx,
            )

        # Extract evidence_ids directly from non-bot blocks
        evidence_ids = [
            non_bot_blocks[i].message_id
            for i in range(span.start_idx, span.end_idx + 1)
            if non_bot_blocks[i].message_id
        ]
        log.info(
            "Case span %s..%s llm_status=%s effective_status=%s evidence_ids=%d",
            span.start_idx, span.end_idx, case.status, effective_status, len(evidence_ids),
        )

        evidence_image_paths = _collect_evidence_image_paths(deps, evidence_ids)

        # Semantic dedup: embed problem text and check similarity against existing cases
        embed_text = f"{case.problem_title}\n{case.problem_summary}"
        dedup_embedding = deps.llm.embed(text=embed_text)
        similar_id = find_similar_case(deps.db, group_id=group_id, embedding=dedup_embedding)
        if similar_id:
            merge_case(
                deps.db,
                target_case_id=similar_id,
                status=effective_status,
                problem_summary=case.problem_summary,
                solution_summary=case.solution_summary,
                tags=case.tags,
                evidence_ids=evidence_ids,
                evidence_image_paths=evidence_image_paths,
            )
            store_case_embedding(deps.db, similar_id, dedup_embedding)
            case_id = similar_id
            log.info("Semantic dedup: merged live case into existing %s (group=%s)", case_id, group_id[:20])
        else:
            case_id = new_case_id(deps.db)
            insert_case(
                deps.db,
                case_id=case_id,
                group_id=group_id,
                status=effective_status,
                problem_title=case.problem_title,
                problem_summary=case.problem_summary,
                solution_summary=case.solution_summary,
                tags=case.tags,
                evidence_ids=evidence_ids,
                evidence_image_paths=evidence_image_paths,
            )
            store_case_embedding(deps.db, case_id, dedup_embedding)

        if effective_status == "solved" and case.solution_summary.strip():
            # Solved case (emoji-confirmed) → index in SCRAG immediately
            doc_text = "\n".join(
                [
                    f"[SOLVED] {case.problem_title.strip()}",
                    f"Проблема: {case.problem_summary.strip()}",
                    f"Рішення: {case.solution_summary.strip()}",
                    "tags: " + ", ".join(case.tags),
                ]
            ).strip()
            embedding = deps.llm.embed(text=doc_text)
            rag_meta: dict = {
                "group_id": group_id,
                "status": "solved",  # in SCRAG, presence means solved
            }
            # Chroma rejects empty list metadata values — only include if non-empty
            if evidence_ids:
                rag_meta["evidence_ids"] = evidence_ids
            if evidence_image_paths:
                rag_meta["evidence_image_paths"] = evidence_image_paths
            deps.rag.upsert_case(
                case_id=case_id,
                document=doc_text,
                embedding=embedding,
                metadata=rag_meta,
            )
            mark_case_in_rag(deps.db, case_id)
            accepted_ranges.append((span.start_idx, span.end_idx))
            log.info("New solved case %s indexed in SCRAG (group=%s)", case_id, group_id[:20])
        else:
            # Open case → store in B1 only, keep messages in buffer
            log.info("New open case %s stored in B1 (not in SCRAG, group=%s)", case_id, group_id[:20])

    # ── Phase 2: Dynamic B1 resolution ────────────────────────────────────────
    # Check if any previously open (B1) cases for this group are now resolved
    # based on the current B2 buffer content.
    try:
        open_cases = get_open_cases_for_group(deps.db, group_id)
        if open_cases:
            for b1_case in open_cases:
                try:
                    resolution = deps.llm.check_case_resolved(
                        case_title=b1_case["problem_title"],
                        case_problem=b1_case["problem_summary"],
                        buffer_text=buf2,
                    )
                    if resolution and resolution.resolved and resolution.solution_summary.strip():
                        # Semantic dedup on promotion: check if a solved case for the
                        # same problem already exists (from history ingest or another live case).
                        promote_embed_text = f"{b1_case['problem_title']}\n{b1_case['problem_summary']}"
                        promote_embedding = deps.llm.embed(text=promote_embed_text)
                        existing_solved = find_similar_case(
                            deps.db,
                            group_id=group_id,
                            embedding=promote_embedding,
                            exclude_case_id=b1_case["case_id"],
                            statuses=["solved"],
                        )

                        if existing_solved:
                            # Merge the new solution into the existing solved case
                            merge_case(
                                deps.db,
                                target_case_id=existing_solved,
                                status="solved",
                                problem_summary=b1_case["problem_summary"],
                                solution_summary=resolution.solution_summary,
                                tags=b1_case.get("tags") or [],
                                evidence_ids=[],
                                evidence_image_paths=[],
                            )
                            store_case_embedding(deps.db, existing_solved, promote_embedding)
                            archive_case(deps.db, b1_case["case_id"])
                            final_case_id = existing_solved
                            log.info(
                                "B1 case %s merged into existing solved %s (group=%s)",
                                b1_case["case_id"], existing_solved, group_id[:20],
                            )
                        else:
                            update_case_to_solved(deps.db, b1_case["case_id"], resolution.solution_summary)
                            store_case_embedding(deps.db, b1_case["case_id"], promote_embedding)
                            final_case_id = b1_case["case_id"]
                            log.info(
                                "B1 case %s promoted to solved (group=%s)",
                                final_case_id, group_id[:20],
                            )

                        doc_text = "\n".join([
                            f"[SOLVED] {b1_case['problem_title'].strip()}",
                            f"Проблема: {b1_case['problem_summary'].strip()}",
                            f"Рішення: {resolution.solution_summary.strip()}",
                            "tags: " + ", ".join(b1_case.get("tags") or []),
                        ]).strip()
                        rag_embedding = deps.llm.embed(text=doc_text)
                        deps.rag.upsert_case(
                            case_id=final_case_id,
                            document=doc_text,
                            embedding=rag_embedding,
                            metadata={
                                "group_id": group_id,
                                "status": "solved",
                            },
                        )
                        mark_case_in_rag(deps.db, final_case_id)
                        log.info(
                            "B1 case %s indexed in SCRAG (group=%s)",
                            final_case_id, group_id[:20],
                        )
                except Exception:
                    log.exception("B1 resolution check failed for case %s", b1_case["case_id"])
    except Exception:
        log.exception("B1 resolution phase failed for group %s", group_id[:20])

    # ── Update buffer: remove message spans that became solved cases ──────────
    if not accepted_ranges:
        set_buffer(deps.db, group_id=group_id, buffer_text=buf2)
        return

    # Map accepted ranges back to message_ids (from non_bot_blocks) so we can
    # remove those messages from the full block list (which also has bot messages).
    consumed_message_ids: set[str] = set()
    for start_idx, end_idx in accepted_ranges:
        for i in range(start_idx, end_idx + 1):
            mid = non_bot_blocks[i].message_id
            if mid:
                consumed_message_ids.add(mid)

    kept_blocks = [b.raw_text for b in blocks if b.message_id not in consumed_message_ids]
    buffer_new = "".join(kept_blocks)

    set_buffer(deps.db, group_id=group_id, buffer_text=buffer_new)
    n_consumed = len(consumed_message_ids)
    log.info(
        "Buffer updated group_id=%s total=%s solved_removed=%s remaining=%s",
        group_id, len(blocks), n_consumed, len(blocks) - n_consumed,
    )


def _handle_maybe_respond(deps: WorkerDeps, payload: Dict[str, Any]) -> None:
    group_id = str(payload["group_id"])
    message_id = str(payload["message_id"])

    msg = get_raw_message(deps.db, message_id=message_id)
    if msg is None:
        log.warning("MAYBE_RESPOND: message not found: %s", message_id)
        return

    # Skip empty messages (system notifications like "user added bot to group")
    if not msg.content_text or not msg.content_text.strip():
        log.info("MAYBE_RESPOND: skipping empty message (likely system notification)")
        return

    # Use Ultimate Agent
    try:
        # Check if group has linked admins (is active)
        from app.db.queries_mysql import get_group_admins, upsert_group_docs, get_admin_session
        admins = get_group_admins(deps.db, group_id)
        active_admin_sessions = [(aid, get_admin_session(deps.db, aid)) for aid in admins]
        active_admins = [aid for aid, sess in active_admin_sessions if sess is not None]
        
        # If no admins are linked to this group, we should not respond.
        # This prevents the bot from spamming groups where it was added but not configured,
        # and also blocks stale groups when admins removed the bot from contacts.
        if not active_admins:
            log.info("Group %s has no active linked admins. Skipping response.", group_id)
            return

        # Get language from first active admin (default to 'uk')
        group_lang = "uk"
        for aid, sess in active_admin_sessions:
            if sess is not None:
                group_lang = sess.lang or "uk"
                break

        # Check for admin commands
        if msg.content_text.strip().startswith("/setdocs"):
            # Check if sender is admin
            sender = str(payload.get("sender") or "")
            
            if sender in active_admins:
                parts = msg.content_text.strip().split()
                if len(parts) > 1:
                    urls = parts[1:]
                    upsert_group_docs(deps.db, group_id, urls)
                    deps.signal.send_group_text(group_id=group_id, text=f"Documentation updated for this group ({len(urls)} URLs).")
                    return
                else:
                    deps.signal.send_group_text(group_id=group_id, text="Usage: /setdocs <url1> <url2> ...")
                    return
            else:
                # Not authorized
                log.warning("User %s tried to set docs but is not admin", sender)
                return

        # Check for bot mention to force response (optional, but good for UX)
        force = _mentions_bot(deps.settings, msg.content_text)

        # ── Gate: fast Flash model decides if this is a genuine support inquiry ──
        # Load recent messages for context (exclude the current one)
        context_msgs = get_last_messages_text(deps.db, group_id, n=9)
        # context_msgs are newest-last; current message is the last item
        context_text = "\n".join(context_msgs[:-1]) if len(context_msgs) > 1 else ""

        # Load images attached to the current message
        gate_images: list[tuple[bytes, str]] | None = None
        if msg.image_paths:
            gate_images = []
            for img_path in msg.image_paths[:2]:
                try:
                    mime, _ = mimetypes.guess_type(img_path)
                    mime = mime or "image/jpeg"
                    with open(img_path, "rb") as fh:
                        gate_images.append((fh.read(), mime))
                except Exception as _img_err:
                    log.debug("Gate: could not load image %s: %s", img_path, _img_err)
            if not gate_images:
                gate_images = None

        try:
            gate = deps.llm.decide_consider(
                message=msg.content_text,
                context=context_text,
                images=gate_images,
            )
            log.info(
                "Gate: consider=%s tag=%s force=%s group=%s",
                gate.consider, gate.tag, force, group_id,
            )
            if not gate.consider and not force:
                log.info("MAYBE_RESPOND: gate filtered message (tag=%s)", gate.tag)
                return
        except Exception as _gate_err:
            log.warning("Gate failed, proceeding without filter: %s", _gate_err)

        raw_answer = deps.ultimate_agent.answer(msg.content_text, group_id=group_id, db=deps.db, lang=group_lang)
        answer = raw_answer

        if answer == "SKIP":
            if force:
                answer = "Вибачте, я не зрозумів запитання або це не стосується моєї компетенції." if group_lang == "uk" else "Sorry, I didn't understand the question or it's outside my expertise."
            else:
                return

        # A "real" RAG answer means the agent found something useful in SCRAG/B1
        # and is not purely escalating to admin.  In that case, suppress B1 case
        # creation for this message in the subsequent BUFFER_UPDATE job.
        rag_answered = (
            raw_answer != "SKIP"
            and raw_answer.strip() != "[[TAG_ADMIN]]"
        )

        mention_recipients = []

        # [[TAG_ADMIN]]: escalate to admin with a notification message
        if answer == "[[TAG_ADMIN]]" or answer.strip() == "[[TAG_ADMIN]]":
            from app.agent.ultimate_agent import detect_lang
            msg_lang = detect_lang(msg.content_text)
            tag_msg = "Потребує уваги адміністратора." if msg_lang == "uk" else "Needs admin attention."
            answer = f"[[MENTION_PLACEHOLDER]] {tag_msg}"
            if active_admins:
                mention_recipients.extend(active_admins)
            else:
                answer = f"@admin {tag_msg}"

        # [[TAG_ADMIN]] embedded inside a longer answer (e.g. from synthesizer)
        elif "[[TAG_ADMIN]]" in answer or "@admin" in answer:
            answer = answer.replace("[[TAG_ADMIN]]", "[[MENTION_PLACEHOLDER]]").replace("@admin", "[[MENTION_PLACEHOLDER]]").strip()
            if active_admins:
                mention_recipients.extend(active_admins)
            else:
                answer = answer.replace("[[MENTION_PLACEHOLDER]]", "@admin")

        # Send response
        quote_author = str(payload.get("sender") or "").strip()
        quote_ts_raw = payload.get("ts")
        quote_ts = int(quote_ts_raw) if quote_ts_raw is not None else int(msg.ts)
        quote_msg = str(payload.get("text") or "").strip()
        
        deps.signal.send_group_text(
            group_id=group_id,
            text=answer,
            quote_timestamp=quote_ts,
            quote_author=quote_author,
            quote_message=quote_msg,
            mention_recipients=mention_recipients,
        )

        # If the bot gave a real RAG answer, mark the user message so the
        # subsequent BUFFER_UPDATE job won't create a B1 open case for it.
        if rag_answered:
            with _rag_answered_lock:
                if len(_rag_answered_messages) >= _RAG_ANSWERED_MAX:
                    try:
                        del _rag_answered_messages[next(iter(_rag_answered_messages))]
                    except StopIteration:
                        pass
                _rag_answered_messages[message_id] = None
            log.debug("Marked message %s as RAG-answered (B1 case creation suppressed)", message_id)
        
    except Exception as e:
        log.exception("Ultimate Agent failed: %s", e)
