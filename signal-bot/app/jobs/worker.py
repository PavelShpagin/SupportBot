from __future__ import annotations

import json
import logging
import mimetypes
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


def _format_buffer_line(msg: RawMessage) -> str:
    reply = f" reply_to={msg.reply_to_id}" if msg.reply_to_id else ""
    return f"{msg.sender_hash} ts={msg.ts}{reply}\n{msg.content_text}\n\n"


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

    line = _format_buffer_line(msg)
    buf = get_buffer(deps.db, group_id=group_id)
    buf2 = (buf or "") + line

    extract = deps.llm.extract_case_from_buffer(buffer_text=buf2)
    if not extract.found:
        set_buffer(deps.db, group_id=group_id, buffer_text=buf2)
        return

    # We extracted exactly one solved case; normalize it into a structured case.
    case = deps.llm.make_case(case_block_text=extract.case_block)
    if not case.keep:
        set_buffer(deps.db, group_id=group_id, buffer_text=extract.buffer_new)
        return

    if case.status == "solved" and not case.solution_summary.strip():
        log.warning("Rejecting solved case without solution_summary")
        set_buffer(deps.db, group_id=group_id, buffer_text=extract.buffer_new)
        return

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

    doc_text = "\n".join(
        [
            case.problem_title.strip(),
            case.problem_summary.strip(),
            case.solution_summary.strip(),
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

    set_buffer(deps.db, group_id=group_id, buffer_text=extract.buffer_new)
    log.info("New case created: case_id=%s group_id=%s", case_id, group_id)


def _handle_maybe_respond(deps: WorkerDeps, payload: Dict[str, Any]) -> None:
    group_id = str(payload["group_id"])
    message_id = str(payload["message_id"])

    msg = get_raw_message(deps.db, message_id=message_id)
    if msg is None:
        log.warning("MAYBE_RESPOND: message not found: %s", message_id)
        return

    context_lines = get_last_messages_text(deps.db, group_id=group_id, n=deps.settings.context_last_n)
    context = "\n".join(context_lines)

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

    # Trust requirement: if we respond, we must reference at least one concrete solution
    # from a solved historical case.
    history_refs = _pick_history_solution_refs(retrieved, max_refs=1)
    if not history_refs:
        log.info("No solved cases with solutions found in retrieval; staying silent")
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
        images=all_images,
    )
    if not resp.respond:
        return

    # Ensure at least one case citation is present (best-effort, even if model forgets).
    required_case_cits = [f'case:{r["case_id"]}' for r in history_refs]
    cits = list(dict.fromkeys((resp.citations or []) + required_case_cits))

    out = resp.text.strip()
    out = _append_history_block(out, history_refs)
    if cits:
        out = out.rstrip() + "\n\nRefs: " + ", ".join(cits[:3]) + "\n"

    # Reply directly to the asker by quoting their message (Signal "reply" UX).
    quote_author = str(payload.get("sender") or "").strip()
    quote_ts_raw = payload.get("ts")
    quote_ts = int(quote_ts_raw) if quote_ts_raw is not None else int(msg.ts)
    quote_msg = str(payload.get("text") or "").strip()

    if quote_author:
        deps.signal.send_group_text(
            group_id=group_id,
            text=out,
            quote_timestamp=quote_ts,
            quote_author=quote_author,
            quote_message=quote_msg if quote_msg else None,
        )
    else:
        deps.signal.send_group_text(group_id=group_id, text=out)

