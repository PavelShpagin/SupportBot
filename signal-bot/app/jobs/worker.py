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
    insert_raw_message,
    get_buffer,
    set_buffer,
    new_case_id,
    insert_case,
    get_last_messages_text,
    get_positive_reactions_for_message,
    get_message_by_ts,
    mark_case_in_rag,
    get_all_active_case_ids,
)
from app.jobs import types as job_types
from app.llm.client import LLMClient
from app.rag.chroma import ChromaRag
from app.signal.adapter import SignalAdapter
from app.agent.ultimate_agent import UltimateAgent

log = logging.getLogger(__name__)

# ── Worker heartbeat ──────────────────────────────────────────────────────────
# Updated every loop iteration so /healthz can detect a stalled worker.
_worker_last_tick: float = time.time()
_worker_tick_lock = threading.Lock()


def _touch_heartbeat() -> None:
    global _worker_last_tick
    with _worker_tick_lock:
        _worker_last_tick = time.time()


def get_worker_heartbeat_age() -> float:
    """Seconds since the worker loop last ticked. Large value = worker is stalled."""
    with _worker_tick_lock:
        return time.time() - _worker_last_tick


# ── Per-job hard timeout ──────────────────────────────────────────────────────
# Any job that does not finish within this window is abandoned (the thread keeps
# running in the background but the main loop moves on and marks the job failed).
# LLM call timeouts (30 s / 60 s) mean the orphaned thread usually terminates
# shortly after the main loop moves on.
# Must be greater than UltimateAgent's as_completed timeout (120s) + synthesizer (45s).
_JOB_TIMEOUT_SECONDS = 180.0


def _run_with_timeout(fn, *args, timeout: float) -> tuple[bool, Exception | None]:
    """Run fn(*args) in a daemon thread with a hard wall-clock timeout.

    Returns (completed, exception_or_None).
    completed=False means the thread is still running (timed out).
    """
    result: dict = {"exc": None}

    def _target() -> None:
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001
            result["exc"] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout)
    return not t.is_alive(), result["exc"]


# ── RAG-answered message tracking ────────────────────────────────────────────
# message_id set: messages for which the bot sent a real RAG answer (not a pure
# [[TAG_ADMIN]] escalation).  When BUFFER_UPDATE processes the buffer afterwards,
# it skips these messages so no duplicate case is created for an already-answered
# question.  Uses an ordered dict so we can do FIFO eviction.
_rag_answered_messages: dict[str, None] = {}

# Track messages we've already sent a response to, so timed-out job retries
# don't produce duplicate messages.  FIFO eviction, same as _rag_answered_messages.
_responded_messages: dict[str, None] = {}
_responded_lock = threading.Lock()
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


_DOC_URL_RE = re.compile(r"https?://docs\.google\.com/document/d/[a-zA-Z0-9_-]+[^\s]*")

_docs_last_checked: dict[str, float] = {}
_DOCS_RECHECK_INTERVAL = 600  # 10 minutes


def sync_docs_from_description(deps: "WorkerDeps", group_id: str, *, force: bool = False) -> None:
    """Extract Google Docs URLs from the group's Signal description and store them.

    Called on every message (throttled to every 10min) and immediately on
    group description change events (force=True).
    """
    now = time.time()
    if not force:
        last = _docs_last_checked.get(group_id, 0.0)
        if now - last < _DOCS_RECHECK_INTERVAL:
            return
    _docs_last_checked[group_id] = now

    from app.db.queries_mysql import get_group_docs, upsert_group_docs

    description = None
    try:
        for g in deps.signal.list_groups():
            if g.group_id == group_id and getattr(g, "description", None):
                description = g.description
                break
    except Exception:
        log.debug("Could not list groups for description sync")
        return

    if not description:
        return

    urls = _DOC_URL_RE.findall(description)
    if not urls:
        return

    existing = get_group_docs(deps.db, group_id)
    if sorted(urls) == sorted(existing):
        return

    log.info("Docs sync: %d URL(s) from group %s description (was %d)", len(urls), group_id[:20], len(existing))
    upsert_group_docs(deps.db, group_id, urls)


def _mentions_bot(settings: Settings, text: str) -> bool:
    low = text.lower()
    return any(m.lower() in low for m in settings.bot_mention_strings)


def _guess_mime(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    return mime or "image/png"


def _is_image_path(path: str) -> bool:
    """Return True if path looks like an image based on MIME type guess."""
    mime, _ = mimetypes.guess_type(path)
    return (mime or "").startswith("image/")


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
            mime = _guess_mime(p)
            if p.startswith("http://") or p.startswith("https://"):
                import requests as _req
                resp = _req.get(p, timeout=15)
                resp.raise_for_status()
                data = resp.content
            else:
                with open(p, "rb") as f:
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


def _expand_evidence_with_gap_attachments(deps: WorkerDeps, group_id: str, evidence_ids: List[str]) -> List[str]:
    """Add messages with attachments that fall in the ts range but weren't in the LLM span."""
    if len(evidence_ids) < 2:
        return evidence_ids

    ts_values = []
    for mid in evidence_ids:
        msg = get_raw_message(deps.db, message_id=mid)
        if msg:
            ts_values.append(msg.ts)
    if len(ts_values) < 2:
        return evidence_ids

    from app.db.queries_mysql import get_messages_in_ts_range
    ts_min, ts_max = min(ts_values), max(ts_values)
    gap_msgs = get_messages_in_ts_range(deps.db, group_id, ts_min, ts_max)
    existing = set(evidence_ids)
    expanded = list(evidence_ids)
    for gm in gap_msgs:
        if gm.message_id not in existing and gm.image_paths:
            expanded.append(gm.message_id)
            log.debug("Added gap message %s with attachments to evidence", gm.message_id)
    return expanded


def _collect_evidence_image_paths(deps: WorkerDeps, evidence_ids: List[str]) -> List[str]:
    """Collect attachment paths from evidence messages."""
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

    # Clear any stale ingesting flags left by a previous crash/OOM kill.
    try:
        with deps.db.connection() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE chat_groups SET ingesting = 0 WHERE ingesting = 1")
            if cur.rowcount:
                log.warning("Cleared %d stale ingesting flags on worker startup", cur.rowcount)
            conn.commit()
    except Exception:
        log.exception("Failed to clear stale ingesting flags on startup")

    last_sync_rag = 0.0

    while True:
        now = time.time()

        # Periodic RAG sync: remove Chroma entries with no matching MySQL case
        if now - last_sync_rag >= _SYNC_RAG_INTERVAL_SECONDS:
            _run_sync_rag(deps)
            last_sync_rag = now

        job = claim_next_job(
            deps.db,
            allowed_types=[job_types.SYNC_GROUP_DOCS, job_types.BUFFER_UPDATE, job_types.MAYBE_RESPOND],
        )
        if job is None:
            _touch_heartbeat()
            time.sleep(deps.settings.worker_poll_seconds)
            continue

        _touch_heartbeat()

        # Defer buffer/respond jobs while a group is being ingested (the swap).
        # Put job back to pending and poll until the flag clears.
        job_group_id = (job.payload or {}).get("group_id", "")
        if job_group_id and job.type in (job_types.BUFFER_UPDATE, job_types.MAYBE_RESPOND):
            from app.db.queries_mysql import is_group_ingesting
            if is_group_ingesting(deps.db, job_group_id):
                log.info("Waiting for ingestion swap to complete — group %s, job %s", job_group_id[:20], job.job_id)
                # Put job back, then wait for flag to clear
                with deps.db.connection() as conn:
                    cur = conn.cursor()
                    cur.execute("UPDATE jobs SET status = 'pending' WHERE job_id = %s", (job.job_id,))
                    conn.commit()
                # Poll with a 5-minute safety timeout (swap should take <1s;
                # if flag is stuck from a crash, force-clear and move on).
                poll_start = time.time()
                while is_group_ingesting(deps.db, job_group_id):
                    if time.time() - poll_start > 300:
                        log.error("Ingesting flag stuck for >5min — force-clearing for group %s", job_group_id[:20])
                        from app.db.queries_mysql import set_group_ingesting
                        set_group_ingesting(deps.db, job_group_id, False)
                        break
                    time.sleep(0.5)
                log.info("Ingestion swap done — resuming jobs for group %s", job_group_id[:20])
                continue

        if job.type == job_types.SYNC_GROUP_DOCS:
            handler = _handle_sync_group_docs
        elif job.type == job_types.BUFFER_UPDATE:
            handler = _handle_buffer_update
        elif job.type == job_types.MAYBE_RESPOND:
            handler = _handle_maybe_respond
        else:
            log.warning("Unknown job type=%s job_id=%s (marking done)", job.type, job.job_id)
            complete_job(deps.db, job_id=job.job_id)
            continue

        completed, exc = _run_with_timeout(
            handler, deps, job.payload, timeout=_JOB_TIMEOUT_SECONDS
        )

        if not completed:
            log.error(
                "Job timed out after %.0fs: id=%s type=%s — marking failed, worker continues",
                _JOB_TIMEOUT_SECONDS, job.job_id, job.type,
            )
            fail_job(deps.db, job_id=job.job_id, attempts=job.attempts)
        elif exc is not None:
            log.exception("Job failed: id=%s type=%s", job.job_id, job.type, exc_info=exc)
            fail_job(deps.db, job_id=job.job_id, attempts=job.attempts)
        else:
            complete_job(deps.db, job_id=job.job_id)


def _index_case_in_rag(
    deps: WorkerDeps,
    *,
    case_id: str,
    group_id: str,
    problem_title: str,
    problem_summary: str,
    solution_summary: str,
    tags: List[str],
    evidence_ids: List[str],
    evidence_image_paths: List[str],
    status: str = "solved",
) -> None:
    """Build the document, embed it, and upsert into ChromaDB immediately."""
    prefix = "[SOLVED]" if status == "solved" else "[РЕКОМЕНДАЦІЯ]"
    doc_text = "\n".join([
        f"{prefix} {problem_title.strip()}",
        f"Проблема: {problem_summary.strip()}",
        f"Рішення: {solution_summary.strip()}",
        "tags: " + ", ".join(tags),
    ]).strip()
    rag_emb = deps.llm.embed(text=doc_text)

    rag_meta: dict = {"group_id": group_id, "status": status}
    if evidence_ids:
        rag_meta["evidence_ids"] = evidence_ids
    if evidence_image_paths:
        rag_meta["evidence_image_paths"] = evidence_image_paths

    deps.rag.upsert_case(
        case_id=case_id,
        document=doc_text,
        embedding=rag_emb,
        metadata=rag_meta,
        status=status,
    )
    mark_case_in_rag(deps.db, case_id)
    log.info("Case %s indexed in %s (group=%s)", case_id, "SCRAG" if status == "solved" else "RCRAG", group_id[:20])


def _handle_sync_group_docs(deps: WorkerDeps, payload: Dict[str, Any]) -> None:
    """Sync docs URLs from group description. Runs before message handling when queued first."""
    group_id = str(payload["group_id"])
    sync_docs_from_description(deps, group_id, force=True)


def _index_aged_out_recommendations(
    deps: WorkerDeps, group_id: str, buffer_message_ids: List[str],
) -> None:
    """Index recommendation cases whose evidence has fully left the buffer into RCRAG."""
    from app.db.queries_mysql import get_recommendation_cases_not_in_rag
    reco_cases = get_recommendation_cases_not_in_rag(deps.db, group_id)
    if not reco_cases:
        return
    buffer_id_set = set(buffer_message_ids)
    for case in reco_cases:
        evidence_ids = case.get("evidence_ids") or []
        overlap = set(evidence_ids) & buffer_id_set
        if not overlap:
            # All evidence aged out of buffer → index in RCRAG
            _index_case_in_rag(
                deps,
                case_id=case["case_id"],
                group_id=group_id,
                problem_title=case["problem_title"],
                problem_summary=case["problem_summary"],
                solution_summary=case["solution_summary"],
                tags=case.get("tags") or [],
                evidence_ids=evidence_ids,
                evidence_image_paths=case.get("evidence_image_paths") or [],
                status="recommendation",
            )
            log.info(
                "Recommendation case %s aged out of buffer, indexed in RCRAG (group=%s)",
                case["case_id"], group_id[:20],
            )


def _build_multimodal_buffer(
    deps: WorkerDeps, extraction_blocks: List[BufferMessageBlock],
) -> tuple[str, list[tuple[bytes, str]] | None]:
    """Build numbered buffer text with [[IMG:N]] markers and collect all images.

    Bot messages ([BOT] tagged) are included for context — the LLM needs to see
    the full conversation flow but is instructed to never create cases from bot responses.
    """
    all_images: list[tuple[bytes, str]] = []
    img_idx = 0
    enriched_parts: list[str] = []

    for b in extraction_blocks:
        block_text = b.raw_text
        # Load images for this message block
        if b.message_id:
            msg_obj = get_raw_message(deps.db, message_id=b.message_id)
            if msg_obj and msg_obj.image_paths:
                msg_images = _load_images(
                    settings=deps.settings,
                    image_paths=[p for p in msg_obj.image_paths if _is_image_path(p)],
                    max_images=2,
                    total_budget_bytes=2 * 1024 * 1024,
                )
                if msg_images:
                    markers = " ".join(f"[[IMG:{img_idx + j}]]" for j in range(len(msg_images)))
                    block_text = block_text.rstrip("\n") + f"\n{markers}\n\n"
                    all_images.extend(msg_images)
                    img_idx += len(msg_images)
                    # Cap total images to avoid excessive prompt size
                    if len(all_images) >= 20:
                        break

        enriched_parts.append(f"### MSG idx={b.idx} lines={b.start_line}-{b.end_line}")
        enriched_parts.append(block_text.rstrip("\n"))
        enriched_parts.append("### END")
        enriched_parts.append("")

    buffer_text = "\n".join(enriched_parts).strip()
    return buffer_text, all_images if all_images else None


def _handle_buffer_update(deps: WorkerDeps, payload: Dict[str, Any]) -> None:
    group_id = str(payload["group_id"])
    message_id = str(payload["message_id"])

    from app.db.queries_mysql import get_group_admins
    admins = get_group_admins(deps.db, group_id)
    if not admins:
        log.info("BUFFER_UPDATE: group %s has no linked admins, skipping", group_id)
        return
    if deps.settings.admin_whitelist and not any(a in deps.settings.admin_whitelist for a in admins):
        log.info("BUFFER_UPDATE: group %s has no whitelisted admins, skipping", group_id)
        return

    msg = get_raw_message(deps.db, message_id=message_id)
    if msg is None:
        log.warning("BUFFER_UPDATE: message not found: %s", message_id)
        return

    positive_reactions = get_positive_reactions_for_message(deps.db, group_id=group_id, target_ts=msg.ts)
    is_bot_msg = bool(deps.bot_sender_hash and msg.sender_hash == deps.bot_sender_hash)
    line = _format_buffer_line(msg, positive_reactions=positive_reactions, is_bot=is_bot_msg)
    buf = get_buffer(deps.db, group_id=group_id)
    buf2 = (buf or "") + line

    buf2 = _trim_buffer(
        buf2,
        max_age_hours=deps.settings.buffer_max_age_hours,
        max_messages=deps.settings.buffer_max_messages,
    )

    blocks = _parse_buffer_blocks(buf2)
    if not blocks:
        set_buffer(deps.db, group_id=group_id, buffer_text=buf2)
        return

    # Exclude RAG-answered messages from extraction (they already spawned cases)
    # Bot messages are KEPT — LLM needs full context but is instructed to never
    # create cases based on bot-only responses (see P_UNIFIED_BUFFER_SYSTEM)
    with _rag_answered_lock:
        local_rag_answered = set(_rag_answered_messages.keys())
    extraction_blocks = [
        b for b in blocks
        if b.message_id not in local_rag_answered
    ]
    if not extraction_blocks:
        set_buffer(deps.db, group_id=group_id, buffer_text=buf2)
        return

    # Load existing solved+recommendation cases overlapping buffer (prevent re-extraction)
    from app.db.queries_mysql import get_overlapping_solved_cases, get_recommendation_cases_for_group
    buffer_msg_ids = [b.message_id for b in blocks if b.message_id]
    existing_cases = get_overlapping_solved_cases(deps.db, group_id, buffer_msg_ids)

    # Load all recommendation cases for promotion checking (LLM checks if buffer confirms them)
    # Deduplicated: existing_cases already tells LLM not to re-extract these
    reco_cases = get_recommendation_cases_for_group(deps.db, group_id)

    # Build multimodal buffer with interleaved images (includes [BOT] messages for context)
    numbered_buffer, all_images = _build_multimodal_buffer(deps, extraction_blocks)

    # ── Single unified LLM call: extract + promote + update ──
    result = deps.llm.unified_buffer_analysis(
        buffer_text=numbered_buffer,
        existing_cases=existing_cases,
        recommendation_cases=reco_cases if reco_cases else None,
        images=all_images,
    )

    # ── Process new cases ──
    n_blocks = len(extraction_blocks)
    for case in result.new_cases:
        if case.start_idx < 0 or case.end_idx >= n_blocks:
            log.warning("Rejecting new case with out-of-range span (%d..%d, n_blocks=%d)",
                        case.start_idx, case.end_idx, n_blocks)
            continue
        if not case.solution_summary.strip():
            continue

        evidence_ids = [
            extraction_blocks[i].message_id
            for i in range(case.start_idx, case.end_idx + 1)
            if extraction_blocks[i].message_id
        ]
        # Also include attachment-bearing messages in the ts gap (LLM often skips them)
        evidence_ids = _expand_evidence_with_gap_attachments(deps, group_id, evidence_ids)
        evidence_image_paths = _collect_evidence_image_paths(deps, evidence_ids)

        case_id = new_case_id(deps.db)
        insert_case(
            deps.db,
            case_id=case_id,
            group_id=group_id,
            status=case.status,
            problem_title=case.problem_title,
            problem_summary=case.problem_summary,
            solution_summary=case.solution_summary,
            tags=case.tags,
            evidence_ids=evidence_ids,
            evidence_image_paths=evidence_image_paths,
        )

        if case.status == "solved":
            _index_case_in_rag(
                deps,
                case_id=case_id,
                group_id=group_id,
                problem_title=case.problem_title,
                problem_summary=case.problem_summary,
                solution_summary=case.solution_summary,
                tags=case.tags,
                evidence_ids=evidence_ids,
                evidence_image_paths=evidence_image_paths,
                status="solved",
            )
            log.info("New solved case %s (span %d..%d, evidence=%d) indexed in SCRAG (group=%s)",
                     case_id, case.start_idx, case.end_idx, len(evidence_ids), group_id[:20])
        else:
            log.info("New recommendation case %s (span %d..%d, evidence=%d) saved in MySQL (group=%s)",
                     case_id, case.start_idx, case.end_idx, len(evidence_ids), group_id[:20])

    # ── Process promotions: recommendation → solved ──
    from app.db.queries_mysql import update_case_to_solved
    reco_by_id = {rc["case_id"]: rc for rc in reco_cases} if reco_cases else {}
    for promo in result.promotions:
        rc = reco_by_id.get(promo.case_id)
        if not rc:
            log.warning("Promotion references unknown case_id=%s, skipping", promo.case_id)
            continue
        solution = promo.solution_summary or rc.get("solution_summary", "")
        update_case_to_solved(deps.db, promo.case_id, solution)
        _index_case_in_rag(
            deps,
            case_id=promo.case_id,
            group_id=group_id,
            problem_title=rc["problem_title"],
            problem_summary=rc["problem_summary"],
            solution_summary=solution,
            tags=rc.get("tags") or [],
            evidence_ids=rc.get("evidence_ids") or [],
            evidence_image_paths=rc.get("evidence_image_paths") or [],
            status="solved",
        )
        log.info("Promoted recommendation case %s → solved (group=%s)", promo.case_id, group_id[:20])

    # ── Process updates to existing cases ──
    from app.db.queries_mysql import get_case, update_case_solution, get_case_evidence_ids
    for upd in result.updates:
        existing = get_case(deps.db, upd.case_id)
        if not existing or not upd.solution_summary.strip():
            continue
        new_evidence_images = _collect_evidence_image_paths(deps, upd.additional_evidence_ids) if upd.additional_evidence_ids else []
        all_evidence_images = list(existing.get("evidence_image_paths") or [])
        for p in new_evidence_images:
            if p not in all_evidence_images:
                all_evidence_images.append(p)
        update_case_solution(deps.db, upd.case_id, upd.solution_summary, new_evidence_ids=upd.additional_evidence_ids or None)
        all_evidence_ids = get_case_evidence_ids(deps.db, upd.case_id)
        # Re-index in RAG with updated solution
        if existing.get("status") == "solved" or (existing.get("status") == "recommendation" and existing.get("in_rag")):
            _index_case_in_rag(
                deps,
                case_id=upd.case_id,
                group_id=group_id,
                problem_title=existing["problem_title"],
                problem_summary=existing["problem_summary"],
                solution_summary=upd.solution_summary,
                tags=existing.get("tags") or [],
                evidence_ids=all_evidence_ids,
                evidence_image_paths=all_evidence_images,
                status=existing["status"],
            )
        log.info("Updated case %s solution + %d new evidence msgs (group=%s)",
                 upd.case_id, len(upd.additional_evidence_ids), group_id[:20])

    # ── Age-out indexing: recommendation cases whose evidence left the buffer ──
    _index_aged_out_recommendations(deps, group_id, buffer_msg_ids)

    # Buffer is append-only: just save the trimmed version, never remove spans.
    set_buffer(deps.db, group_id=group_id, buffer_text=buf2)


def _handle_maybe_respond(deps: WorkerDeps, payload: Dict[str, Any]) -> None:
    group_id = str(payload["group_id"])
    message_id = str(payload["message_id"])

    # Dedup: skip if we already responded to this message (e.g. timed-out job retry)
    with _responded_lock:
        if message_id in _responded_messages:
            log.info("MAYBE_RESPOND: already responded to %s — skipping duplicate", message_id)
            return

    msg = get_raw_message(deps.db, message_id=message_id)
    if msg is None:
        log.warning("MAYBE_RESPOND: message not found: %s", message_id)
        return

    # Skip empty messages (system notifications like "user added bot to group")
    # But keep messages that have media attachments (image_paths) even if text is empty
    if (not msg.content_text or not msg.content_text.strip()) and not msg.image_paths:
        log.info("MAYBE_RESPOND: skipping empty message (likely system notification)")
        return

    # Use Ultimate Agent
    try:
        # Check if group has linked admins (is active) and at least one is whitelisted
        from app.db.queries_mysql import get_group_admins, upsert_group_docs, get_admin_session
        admins = get_group_admins(deps.db, group_id)
        active_admin_sessions = [(aid, get_admin_session(deps.db, aid)) for aid in admins]
        active_admins = [aid for aid, sess in active_admin_sessions if sess is not None]

        # If no admins are linked to this group, we should not respond.
        if not active_admins:
            log.info("Group %s has no active linked admins. Skipping response.", group_id)
            return
        if deps.settings.admin_whitelist and not any(a in deps.settings.admin_whitelist for a in active_admins):
            log.info("Group %s has no whitelisted admins. Skipping response.", group_id)
            return

        # Get language from first active admin (default to 'uk')
        group_lang = "uk"
        for aid, sess in active_admin_sessions:
            if sess is not None:
                group_lang = sess.lang or "uk"
                break

        # Auto-parse docs URLs from group description if none configured yet
        sync_docs_from_description(deps, group_id)

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
        context_msgs = get_last_messages_text(deps.db, group_id, n=deps.settings.context_last_n, bot_sender_hash=deps.bot_sender_hash)
        # context_msgs are newest-last; current message is the last item
        context_text = "\n".join(context_msgs[:-1]) if len(context_msgs) > 1 else ""

        # Load images attached to the current message and embed markers in message text
        gate_images: list[tuple[bytes, str]] | None = None
        gate_message_text = msg.content_text
        if msg.image_paths:
            loaded = _load_images(
                settings=deps.settings,
                image_paths=[p for p in msg.image_paths if _is_image_path(p)],
                max_images=2,
                total_budget_bytes=deps.settings.max_total_image_bytes,
            )
            if loaded:
                gate_images = loaded
                markers = " ".join(f"[[IMG:{j}]]" for j in range(len(loaded)))
                gate_message_text = f"{msg.content_text}\n{markers}"

        gate_tag = ""  # populated below if gate succeeds
        try:
            gate = deps.llm.decide_consider(
                message=gate_message_text,
                context=context_text,
                images=gate_images,
            )
            gate_tag = gate.tag or ""
            log.info(
                "Gate: consider=%s tag=%s force=%s group=%s",
                gate.consider, gate_tag, force, group_id,
            )
            if not gate.consider and not force:
                log.info("MAYBE_RESPOND: gate filtered message (tag=%s)", gate_tag)
                return
        except Exception as _gate_err:
            log.warning("Gate failed, proceeding without filter: %s", _gate_err)

        raw_answer = deps.ultimate_agent.answer(
            gate_message_text, group_id=group_id, db=deps.db, lang=group_lang,
            context=context_text, images=gate_images, gate_tag=gate_tag,
        )
        answer_text = raw_answer.text
        attachment_urls = raw_answer.attachment_urls

        # ── Pre-send context check: if new messages arrived during synthesis, re-synthesize ──
        if not force:
            try:
                fresh_msgs = get_last_messages_text(deps.db, group_id, n=deps.settings.context_last_n, bot_sender_hash=deps.bot_sender_hash)
                fresh_context = "\n".join(fresh_msgs)
                original_context = "\n".join(context_msgs)
                if fresh_context != original_context:
                    # Context changed — new messages arrived during synthesis.
                    # Re-run ONLY the synthesizer with updated context (sub-agent results reused).
                    log.info("MAYBE_RESPOND: context changed during synthesis, re-synthesizing for message %s", message_id)
                    fresh_context_text = "\n".join(fresh_msgs[:-1]) if len(fresh_msgs) > 1 else ""
                    raw_answer = deps.ultimate_agent.re_synthesize(
                        gate_message_text, new_context=fresh_context_text,
                        prev_response=raw_answer, db=deps.db, images=gate_images,
                    )
                    answer_text = raw_answer.text
                    attachment_urls = raw_answer.attachment_urls
                    log.info("MAYBE_RESPOND: re-synthesis result: %s", answer_text[:80] if answer_text else "empty")
            except Exception as _resynth_err:
                log.warning("Pre-send re-synthesis failed, proceeding with original: %s", _resynth_err)

        if answer_text == "SKIP":
            if force:
                answer_text = "Вибачте, я не зрозумів запитання або це не стосується моєї компетенції." if group_lang == "uk" else "Sorry, I didn't understand the question or it's outside my expertise."
            else:
                return

        rag_answered = (
            answer_text != "SKIP"
            and answer_text.strip() != "[[TAG_ADMIN]]"
        )

        mention_recipients = []

        # Check per-group tag targets (set via /tag command)
        from app.db.queries_mysql import get_tag_targets
        tag_targets = get_tag_targets(deps.db, group_id)

        if answer_text == "[[TAG_ADMIN]]" or answer_text.strip() == "[[TAG_ADMIN]]":
            from app.agent.ultimate_agent import detect_lang
            msg_lang = detect_lang(msg.content_text)
            tag_msg = "Потребує уваги адміністратора." if msg_lang == "uk" else "Needs admin attention."
            answer_text = f"[[MENTION_PLACEHOLDER]] {tag_msg}"
            if tag_targets:
                mention_recipients.extend(tag_targets)
            elif active_admins:
                mention_recipients.extend(active_admins)
            else:
                answer_text = f"@admin {tag_msg}"

        elif "[[TAG_ADMIN]]" in answer_text or "@admin" in answer_text:
            answer_text = answer_text.replace("[[TAG_ADMIN]]", "[[MENTION_PLACEHOLDER]]").replace("@admin", "[[MENTION_PLACEHOLDER]]").strip()
            if tag_targets:
                mention_recipients.extend(tag_targets)
                log.info("MENTION: using tag_targets=%s", tag_targets)
            elif active_admins:
                mention_recipients.extend(active_admins)
                log.info("MENTION: using active_admins=%s", active_admins)
            else:
                answer_text = answer_text.replace("[[MENTION_PLACEHOLDER]]", "@admin")
                log.info("MENTION: no recipients, falling back to @admin text")

        if rag_answered:
            with _rag_answered_lock:
                if len(_rag_answered_messages) >= _RAG_ANSWERED_MAX:
                    try:
                        del _rag_answered_messages[next(iter(_rag_answered_messages))]
                    except StopIteration:
                        pass
                _rag_answered_messages[message_id] = None
            log.debug("Marked message %s as RAG-answered (case creation suppressed)", message_id)

        # Clean answer_text for storage: replace placeholders with readable text
        stored_text = answer_text.replace("[[MENTION_PLACEHOLDER]]", "@admin")

        # Send text response
        quote_author = str(payload.get("sender") or "").strip()
        quote_ts_raw = payload.get("ts")
        quote_ts = int(quote_ts_raw) if quote_ts_raw is not None else int(msg.ts)
        quote_msg = str(payload.get("text") or "").strip()

        # Mark as responded BEFORE sending — prevents duplicate if timeout retry
        with _responded_lock:
            if len(_responded_messages) >= _RAG_ANSWERED_MAX:
                try:
                    del _responded_messages[next(iter(_responded_messages))]
                except StopIteration:
                    pass
            _responded_messages[message_id] = None

        log.info("SEND: mention_recipients=%s, has_placeholder=%s", mention_recipients, "[[MENTION_PLACEHOLDER]]" in answer_text)
        try:
            sent_ts = deps.signal.send_group_text(
                group_id=group_id,
                text=answer_text,
                quote_timestamp=quote_ts,
                quote_author=quote_author,
                quote_message=quote_msg,
                mention_recipients=mention_recipients,
            )
        except RuntimeError:
            # Retry without quote — quote_author may be unregistered
            log.warning("SEND: failed with quote, retrying without quote")
            sent_ts = deps.signal.send_group_text(
                group_id=group_id,
                text=answer_text,
                mention_recipients=mention_recipients,
            )

        # Store bot response in raw_messages so future context includes it
        if sent_ts and deps.bot_sender_hash:
            bot_msg = RawMessage(
                message_id=str(sent_ts),
                group_id=group_id,
                ts=sent_ts,
                sender_hash=deps.bot_sender_hash,
                content_text=stored_text,
                image_paths=[],
                reply_to_id=msg.message_id,
                sender_name="BOT",
            )
            insert_raw_message(deps.db, bot_msg)
            # Also append to buffer with [BOT] tag
            bot_line = _format_buffer_line(bot_msg, is_bot=True)
            buf = get_buffer(deps.db, group_id=group_id)
            set_buffer(deps.db, group_id=group_id, buffer_text=(buf or "") + bot_line)
            log.info("Stored bot response in raw_messages ts=%s and appended to buffer", sent_ts)

        # Send file attachments if any
        if attachment_urls:
            import tempfile
            import app.r2 as _r2
            for att_url in attachment_urls[:3]:
                try:
                    r2_key = _r2.key_from_url(att_url)
                    if r2_key:
                        result = _r2.download(r2_key)
                        att_bytes = result[0] if result else None
                    else:
                        att_bytes = None
                    if att_bytes is None:
                        log.warning("Could not download attachment for sharing: %s", att_url)
                        continue
                    fname = att_url.rsplit('/', 1)[-1] if '/' in att_url else "attachment"
                    with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{fname}") as tmp:
                        tmp.write(att_bytes)
                        tmp_path = tmp.name
                    deps.signal.send_group_attachment(group_id=group_id, file_path=tmp_path)
                    try:
                        import os
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                except Exception:
                    log.exception("Failed to send attachment %s", att_url)
        
    except Exception as e:
        log.exception("Ultimate Agent failed: %s", e)
