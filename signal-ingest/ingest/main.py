"""
Signal History Ingestion Service

Uses Signal Desktop with QR-based user linking to:
1. Reset Signal Desktop and generate QR code
2. Wait for user to scan QR (links their account temporarily)
3. Read historical messages from their groups (45-day sync)
4. Extract solved support cases using LLM
5. Post cases to signal-bot for RAG indexing
6. Reset Signal Desktop for next user

Each history import requires the user to scan a QR code.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from openai import OpenAI

from ingest.config import load_settings
from ingest.db import claim_next_job, complete_job, create_db, fail_job, is_job_cancelled

HISTORY_LINK = "HISTORY_LINK"
HISTORY_SYNC = "HISTORY_SYNC"


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

# Valid single-character escape sequences in JSON
_VALID_ESCAPES = set('"\\' + "/bfnrtu")

def _safe_json_loads(raw: str) -> dict:
    """Parse a JSON string from an LLM, tolerating invalid backslash escapes.

    LLM responses (especially for Cyrillic text with file paths, regex patterns,
    or Windows-style paths) occasionally contain bare backslashes that aren't
    valid JSON escape sequences (e.g. ``\\d``, ``\\p``, ``\\U`` without proper
    Unicode digits).  ``json.loads`` raises ``JSONDecodeError`` on these.

    This function retries with a two-stage repair:
    1. Replace invalid ``\\X`` sequences with ``\\\\X`` (escaped backslash).
    2. If that still fails, strip control characters and retry.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Stage 1: fix invalid escape sequences
    def _fix_escapes(m: re.Match) -> str:
        char = m.group(1)
        return "\\\\" + char if char not in _VALID_ESCAPES else m.group(0)

    fixed = re.sub(r"\\(.)", _fix_escapes, raw)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    # Stage 2: strip non-printable ASCII control characters (except tab/newline)
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", fixed)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        log.warning("LLM JSON parse failed even after repair: %s", e)
        return {}

P_BLOCKS_SYSTEM = """You analyze a chunk of support chat history and extract FULLY RESOLVED support cases.

Each message in the chunk is formatted as:
  sender_hash ts=TIMESTAMP msg_id=MESSAGE_ID
  message text

Return ONLY valid JSON with key:
- cases: array of objects, each with:
  - case_block: string (the EXACT messages from the chunk that form this case, problem through resolution, preserving all header lines with msg_id)

Rules:
- Extract ONLY solved cases with a confirmed working solution.
- Do NOT extract open/unresolved issues, greetings, or off-topic messages.
- Each case_block must include both the problem and the confirmed solution.
- Preserve the original message headers (sender_hash ts=... msg_id=...) verbatim inside case_block ? they are needed for evidence linking.
- Do not paraphrase or summarize; copy the exact message lines.
- If there are no solved cases, return {"cases": []}.

Resolution signals (from strongest to weakest):
- reactions=N (N > 0) on a technical answer message -- STRONG signal, treat as confirmed resolved
- Text confirmation after a technical answer (any language):
  English: "thanks", "working", "works", "ok", "solved", "it worked", "fixed"
  Ukrainian: "\u0434\u044f\u043a\u0443\u044e", "\u043f\u0440\u0430\u0446\u044e\u0454", "\u0432\u0438\u0440\u0456\u0448\u0435\u043d\u043e", "\u043e\u043a", "\u0437\u0430\u0440\u0430\u0431\u043e\u0442\u0430\u043b\u043e"
  Russian: "\u0441\u043f\u0430\u0441\u0438\u0431\u043e", "\u0437\u0430\u0440\u0430\u0431\u043e\u0442\u0430\u043b\u043e", "\u043f\u043e\u043c\u043e\u0433\u043b\u043e"
- The conversation thread ends after a technical answer (no follow-up questions)

Be generous: if a technical answer has any positive reaction OR brief confirmation, treat as solved.
"""

P_BLOCKS_STRUCTURED = """You analyze a chunk of support chat history and extract FULLY RESOLVED support cases with structured fields.

Each message in the chunk is formatted as:
  sender_hash ts=TIMESTAMP msg_id=MESSAGE_ID [reactions=N] [reaction_emoji=X]
  message text

Return ONLY valid JSON with key:
- cases: array of objects, each with:
  - keep: boolean (true for real support cases)
  - status: "solved" or "open"
  - problem_title: string (4-10 words, Ukrainian)
  - problem_summary: string (2-5 lines, concrete, Ukrainian)
  - solution_summary: string (1-10 lines; required if solved, Ukrainian)
  - tags: array of 3-8 short technical tags (can be English)
  - evidence_ids: array of ALL msg_id values from message headers in this case
  - case_block: string (the EXACT messages from the chunk, preserving headers with msg_id)

Rules:
- Extract ONLY solved cases with a confirmed working solution. keep=false for bot-only or no human answer.
- Preserve original headers (sender_hash ts=... msg_id=...) verbatim inside case_block.
- Resolution signals: reactions=N>0, "thanks"/"works"/"ok", or thread ends after technical answer.
- problem_title, problem_summary, solution_summary in Ukrainian.
- evidence_ids: extract ALL msg_id=XXX from headers.
- If no solved cases, return {"cases": []}.
"""

P_DEDUP_CASES = """You receive a list of extracted support cases. Some may be duplicates (same problem, different wording from chunk overlap).
Merge duplicate cases into one. When merging: combine evidence_ids, keep the richer problem_summary/solution_summary, merge tags.
Return ONLY valid JSON:
- cases: array of merged case objects, each with: keep, status, problem_title, problem_summary, solution_summary, tags, evidence_ids, case_block
Preserve the exact schema. Do not drop valid cases. Only merge obvious duplicates (same root cause)."""

log = logging.getLogger(__name__)


class JobCancelled(Exception):
    """Raised when job was cancelled by user."""
    pass


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


# ?????????????????????????????????????????????????????????????????????????????
# Signal Desktop Operations
# ?????????????????????????????????????????????????????????????????????????????

def _check_desktop_status(settings) -> dict:
    """Check if Signal Desktop is linked and available."""
    url = settings.signal_desktop_url.rstrip("/") + "/status"
    try:
        with httpx.Client(timeout=30) as client:
            r = client.get(url)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        log.exception("Failed to check Signal Desktop status")
        raise RuntimeError(f"Signal Desktop not available: {e}")


def _reset_desktop(settings) -> dict:
    """Reset Signal Desktop to show QR code for new user linking."""
    url = settings.signal_desktop_url.rstrip("/") + "/reset"
    try:
        with httpx.Client(timeout=60) as client:
            r = client.post(url)
            # Reset may return 500 on permission errors but still work
            return r.json() if r.status_code == 200 else {"status": "reset_attempted"}
    except Exception as e:
        log.warning("Reset request failed (may still work): %s", e)
        return {"status": "reset_attempted"}


def _get_desktop_screenshot(settings) -> bytes:
    """Get screenshot from Signal Desktop (for QR code)."""
    url = settings.signal_desktop_url.rstrip("/") + "/screenshot"
    try:
        with httpx.Client(timeout=30) as client:
            r = client.get(url)
            r.raise_for_status()
            return r.content
    except Exception as e:
        log.exception("Failed to get screenshot")
        raise RuntimeError(f"Failed to get screenshot: {e}")



def _fetch_attachment(
    settings,
    rel_path: str,
    cdn_key: str = "",
    max_bytes: int = 5_000_000,
) -> Optional[bytes]:
    """Fetch raw bytes for a Signal Desktop attachment.

    Tries two sources in order:
    1. ``GET /attachment?path=...`` — for attachments already on disk in
       Signal Desktop's ``attachments.noindex/`` directory.
    2. ``GET /attachment/by-cdn/{cdn_key}`` — for attachments downloaded
       directly from Signal's CDN via ``POST /attachments/fetch-all``.

    Returns None if both sources fail or the file is too large.
    """
    base = settings.signal_desktop_url.rstrip("/")

    def _get(url: str, **kw) -> Optional[bytes]:
        try:
            with httpx.Client(timeout=30) as client:
                r = client.get(url, **kw)
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                if len(r.content) > max_bytes:
                    log.warning("Attachment too large (%d bytes), skipping", len(r.content))
                    return None
                return r.content
        except Exception as e:
            log.warning("Failed to fetch attachment from %s: %s", url, e)
            return None

    if rel_path:
        data = _get(f"{base}/attachment", params={"path": rel_path})
        if data is not None:
            return data
        # Fall through to CDN cache if the disk file isn't there yet

    if cdn_key:
        data = _get(f"{base}/attachment/by-cdn/{cdn_key}")
        if data is not None:
            return data

    if rel_path or cdn_key:
        log.warning(
            "Attachment not found via path=%r cdn_key=%s",
            rel_path or "(none)", cdn_key or "(none)",
        )
    return None


def _ocr_attachment(
    openai_client: OpenAI,
    model: str,
    image_bytes: bytes,
    content_type: str,
    context_text: str = "",
) -> str:
    """Run multimodal OCR on an image and return a structured description.

    Extracts all visible text verbatim and a concise functional description
    of what the image shows in the context of the support conversation.
    Returns an empty string on failure (the message is still stored without OCR).
    """
    try:
        import base64 as _b64
        b64 = _b64.b64encode(image_bytes).decode("utf-8")
        context_hint = f'\nThe message this image was attached to says: "{context_text}"' if context_text else ""
        prompt = (
            "You are analyzing a screenshot or photo shared in a technical support chat.{context_hint}\n\n"
            "Your job:\n"
            "1. Extract ALL visible text verbatim (error messages, settings values, labels, numbers, "
            "filenames, log lines — everything readable). Do not paraphrase.\n"
            "2. Write a concise functional description: what screen/state is shown, what problem or "
            "information it illustrates, and how it relates to the support conversation.\n\n"
            "Return a JSON object with exactly two keys:\n"
            '  "extracted_text": string with all visible text copied verbatim (empty string if none),\n'
            '  "description": one or two sentences describing what the image shows and its relevance.\n'
            "Return only valid JSON, no markdown fences."
        ).format(context_hint=context_hint)
        messages: List[Dict] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{content_type};base64,{b64}"},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        resp = openai_client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=1024,
            temperature=0,
        )
        return resp.choices[0].message.content or ""
    except Exception as e:
        log.warning("OCR failed for attachment: %s", e)
        return ""


def _fetch_attachments_direct(
    settings,
    group_id: str,
    group_name: str,
    timeout: int = 120,
) -> dict:
    """Download all pending group attachments directly from Signal's CDN.

    Calls ``POST /attachments/fetch-all`` on signal-desktop, which reads
    attachment CDN metadata from the SQLite DB, downloads each encrypted blob
    from ``cdn{N}.signal.org``, decrypts it (AES-256-CBC + HMAC-SHA256), and
    caches the plaintext at ``{signal_data_dir}/cdn-cache/{cdnKey}``.

    This replaces the old CDP-trigger + polling-wait approach entirely.
    Returns the response dict: ``{downloaded, failed, skipped}``.
    """
    url = settings.signal_desktop_url.rstrip("/") + "/attachments/fetch-all"
    params = {"group_id": group_id, "group_name": group_name}
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, params=params)
            r.raise_for_status()
            result = r.json()
            log.info(
                "Direct CDN fetch: downloaded=%s failed=%s skipped=%s",
                result.get("downloaded"), result.get("failed"), result.get("skipped"),
            )
            return result
    except Exception as e:
        log.warning("Direct CDN fetch failed (non-fatal): %s", e)
        return {"downloaded": 0, "failed": 0, "skipped": 0, "error": str(e)}


def _get_desktop_messages(settings, group_id: str, group_name: str, limit: int = 800) -> List[dict]:
    """Get messages from Signal Desktop for a specific group."""
    url = settings.signal_desktop_url.rstrip("/") + "/group/messages"
    params = {"group_id": group_id, "limit": limit, "group_name": group_name}
    
    try:
        with httpx.Client(timeout=120) as client:
            r = client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            messages = data.get("messages", [])
            # Log reaction stats
            with_reactions = [m for m in messages if m.get("reactions", 0) > 0]
            if with_reactions:
                log.info("Messages with reactions: %d/%d", len(with_reactions), len(messages))
                for m in with_reactions[:3]:
                    log.info("  reactions=%d: %s", m.get("reactions"), m.get("body", "")[:50])
            else:
                log.info("No messages with reactions found (total: %d)", len(messages))
            return messages
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            log.warning("Group not found in Signal Desktop: %s (%s)", group_name, group_id)
            return []
        raise


# ?????????????????????????????????????????????????????????????????????????????
# LLM Case Extraction
# ?????????????????????????????????????????????????????????????????????????????

def _enrich_messages_with_attachments(
    *,
    settings,
    openai_client: OpenAI,
    messages: List[dict],
    max_att_per_message: int = 3,
) -> List[dict]:
    """Fetch attachment bytes for each message, OCR them, and enrich the body.

    Each message dict is copied with two new keys:
    - ``enriched_body``: original body + appended OCR JSON for each attachment.
    - ``image_payloads``: list of ``{filename, content_type, data_b64}`` dicts
      for storage in signal-bot's raw_messages.image_paths_json.

    Messages without attachments pass through unchanged (but gain empty keys).
    """
    enriched: List[dict] = []
    att_count = 0
    for m in messages:
        atts = m.get("attachments") or []
        if not atts:
            enriched.append({**m, "enriched_body": m.get("body") or m.get("text") or "", "image_payloads": []})
            continue

        body = m.get("body") or m.get("text") or ""
        payloads: List[dict] = []
        ocr_texts: List[str] = []

        for att in atts[:max_att_per_message]:
            rel_path = att.get("path") or ""
            cdn_key = att.get("cdnKey") or ""
            if not rel_path and not cdn_key:
                continue
            content_type = att.get("contentType") or "application/octet-stream"

            att_bytes = _fetch_attachment(settings, rel_path, cdn_key=cdn_key)
            if att_bytes is None:
                continue

            att_count += 1

            # OCR only for images
            if content_type.startswith("image/"):
                ocr_json = _ocr_attachment(
                    openai_client=openai_client,
                    model=settings.model_img,
                    image_bytes=att_bytes,
                    content_type=content_type,
                    context_text=body,
                )
                if ocr_json:
                    ocr_texts.append(ocr_json)
            else:
                # Non-image: note the filename in the enriched body so LLM knows it exists
                fname = att.get("fileName") or rel_path.split("/")[-1] or "attachment"
                ocr_texts.append(f'[attachment: {fname} ({content_type})]')

            payloads.append({
                "filename": att.get("fileName") or "",
                "content_type": content_type,
                "data_b64": base64.b64encode(att_bytes).decode("utf-8"),
            })

        enriched_body = body
        if ocr_texts:
            enriched_body = body + "\n\n" + "\n".join(f"[image]\n{t}" for t in ocr_texts)

        enriched.append({**m, "enriched_body": enriched_body, "image_payloads": payloads})

    if att_count:
        log.info("Processed %d image attachments across %d messages", att_count, len(messages))
    return enriched


def _is_bot_message(text: str, sender: str, bot_e164: str) -> bool:
    """Return True if this message was sent by the bot (should be excluded from extraction)."""
    if bot_e164 and sender == bot_e164:
        return True
    # Detect bot messages by their content: they always include a supportbot.info case link
    if "supportbot.info/case/" in (text or ""):
        return True
    return False


def _chunk_messages(*, messages: List[dict], max_chars: int, overlap_messages: int,
                    bot_e164: str = "") -> List[str]:
    """Split messages into chunks for LLM processing. Bot messages are excluded."""
    formatted = []
    for m in messages:
        # Prefer enriched_body which includes OCR text for images
        text = m.get("enriched_body") or m.get("text") or m.get("body") or ""
        if not text:
            continue
        sender = m.get("sender") or m.get("source") or "unknown"
        if _is_bot_message(text, sender, bot_e164):
            continue  # Never feed bot auto-responses to the extraction LLM
        ts = m.get("ts") or m.get("timestamp") or 0
        msg_id = m.get("id") or m.get("message_id") or str(ts)
        reactions = int(m.get("reactions") or 0)
        header = f'{sender} ts={ts} msg_id={msg_id}'
        if reactions > 0:
            header += f' reactions={reactions}'
            rxn_emoji = m.get("reaction_emoji") or ""
            if rxn_emoji:
                header += f' reaction_emoji={rxn_emoji}'
        formatted.append(f'{header}\n{text}\n')
    
    chunks: List[str] = []
    cur: List[str] = []
    
    for line in formatted:
        candidate = "".join(cur) + line
        if len(candidate) > max_chars and cur:
            chunks.append("".join(cur))
            cur = cur[-overlap_messages:] if overlap_messages > 0 else []
        cur.append(line)
    
    if cur:
        chunks.append("".join(cur))
    
    return chunks


def _llm_call_with_fallback(
    *,
    openai_client: OpenAI,
    model: str,
    fallback_models: list,
    timeout: float,
    **kwargs,
):
    """Call openai_client.chat.completions.create with model cascade on 503/timeout.

    Tries `model` first, then each entry in `fallback_models` in order.
    Only falls back on transient errors (503 Service Unavailable, timeout).
    Raises the last exception if all models are exhausted.
    """
    import openai as _openai

    models_to_try = [model] + list(fallback_models)
    last_exc: Exception | None = None
    for m in models_to_try:
        try:
            return openai_client.chat.completions.create(model=m, timeout=timeout, **kwargs)
        except (_openai.APITimeoutError, _openai.InternalServerError) as e:
            is_503 = isinstance(e, _openai.InternalServerError) and getattr(e, "status_code", None) == 503
            if isinstance(e, _openai.APITimeoutError) or is_503:
                log.warning("Model %s failed (%s), trying next fallback...", m, type(e).__name__)
                last_exc = e
                time.sleep(2)
            else:
                raise
    raise last_exc


def _extract_case_blocks(
    *,
    openai_client: OpenAI,
    model: str,
    chunk_text: str,
    timeout: float = 120.0,
) -> List[str]:
    """Extract solved support cases from a chunk of messages.

    Uses text-only input (no images). Timeout prevents indefinite hangs
    on API rate limits or slow responses (e.g. second chunk in batch).
    """
    for attempt in range(2):
        try:
            resp = openai_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": P_BLOCKS_SYSTEM},
                    {"role": "user", "content": f"HISTORY_CHUNK:\n{chunk_text}"},
                ],
                response_format={"type": "json_object"},
                temperature=0,
                timeout=timeout,
            )
            break
        except Exception as e:
            if attempt == 0:
                log.warning("Chunk extract attempt 1 failed: %s, retrying in 5s...", e)
                time.sleep(5)
            else:
                raise
    raw = resp.choices[0].message.content or "{}"
    data = _safe_json_loads(raw)
    out: List[str] = []
    cases = data.get("cases", [])
    if isinstance(cases, list):
        for c in cases:
            if isinstance(c, dict) and isinstance(c.get("case_block"), str) and c["case_block"].strip():
                out.append(c["case_block"].strip())
    return out


def _extract_structured_cases(
    *,
    openai_client: OpenAI,
    model: str,
    fallback_models: list,
    chunk_text: str,
    timeout: float = 120.0,
) -> List[dict]:
    """Extract solved support cases with full structured fields in one pass."""
    resp = _llm_call_with_fallback(
        openai_client=openai_client,
        model=model,
        fallback_models=fallback_models,
        timeout=timeout,
        messages=[
            {"role": "system", "content": P_BLOCKS_STRUCTURED},
            {"role": "user", "content": f"HISTORY_CHUNK:\n{chunk_text}"},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    raw = resp.choices[0].message.content or "{}"
    data = _safe_json_loads(raw)
    out: List[dict] = []
    cases = data.get("cases", [])
    if isinstance(cases, list):
        for c in cases:
            if (
                isinstance(c, dict)
                and c.get("keep") is True
                and isinstance(c.get("case_block"), str)
                and c["case_block"].strip()
            ):
                out.append({
                    "keep": True,
                    "status": c.get("status") or "solved",
                    "problem_title": (c.get("problem_title") or "").strip(),
                    "problem_summary": (c.get("problem_summary") or "").strip(),
                    "solution_summary": (c.get("solution_summary") or "").strip(),
                    "tags": c.get("tags") or [],
                    "evidence_ids": c.get("evidence_ids") or [],
                    "case_block": c["case_block"].strip(),
                })
    return out


def _dedup_cases_llm(
    *,
    openai_client: OpenAI,
    model: str,
    fallback_models: list,
    cases: List[dict],
    timeout: float = 120.0,
) -> List[dict]:
    """Merge duplicate cases via LLM. Returns deduplicated list."""
    if len(cases) <= 1:
        return cases
    cases_json = json.dumps([{k: c.get(k) for k in ("keep", "status", "problem_title", "problem_summary", "solution_summary", "tags", "evidence_ids", "case_block")} for c in cases], ensure_ascii=False)
    resp = _llm_call_with_fallback(
        openai_client=openai_client,
        model=model,
        fallback_models=fallback_models,
        timeout=timeout,
        messages=[
            {"role": "system", "content": P_DEDUP_CASES},
            {"role": "user", "content": f"CASES:\n{cases_json}"},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    raw = resp.choices[0].message.content or "{}"
    data = _safe_json_loads(raw)
    if not data:
        return cases
    merged = data.get("cases", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    if isinstance(merged, list) and merged:
        return [c for c in merged if isinstance(c, dict) and c.get("case_block")]
    return cases


# ?????????????????????????????????????????????????????????????????????????????
# Signal-Bot Communication
# ?????????????????????????????????????????????????????????????????????????????

def _notify_progress(*, settings, token: str, progress_key: str, **kwargs) -> None:
    """Send progress update to signal-bot."""
    payload = {"token": token, "progress_key": progress_key, **kwargs}
    url = settings.signal_bot_url.rstrip("/") + "/history/progress"
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
    except Exception:
        log.exception("Failed to notify progress")


def _notify_link_result(*, settings, token: str, success: bool, message_count: int = 0, cases_found: int = 0, cases_inserted: int | None = None, note: str = "") -> None:
    """Notify signal-bot of link success/failure."""
    payload = {
        "token": token,
        "success": success,
        "message_count": message_count,
        "cases_found": cases_found,
        "note": note,
    }
    if cases_inserted is not None:
        payload["cases_inserted"] = cases_inserted
    url = settings.signal_bot_url.rstrip("/") + "/history/link-result"
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
            log.info("Notified link result: success=%s messages=%d cases=%d", success, message_count, cases_found)
    except Exception:
        log.exception("Failed to notify link result")


def _send_qr_to_user(*, settings, token: str, qr_image: bytes) -> bool:
    """Send QR code image to user via signal-bot."""
    import base64
    payload = {
        "token": token,
        "qr_image_base64": base64.b64encode(qr_image).decode("utf-8"),
    }
    url = settings.signal_bot_url.rstrip("/") + "/history/qr-code"
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
            log.info("Sent QR code to user")
            return True
    except Exception:
        log.exception("Failed to send QR code to user")
        return False


def _post_cases_to_bot(*, settings, token: str, group_id: str, case_blocks: List[str], messages: List[dict]) -> int:
    """Post extracted cases to signal-bot for RAG indexing. Returns cases_inserted from response."""
    import hashlib
    bot_e164 = settings.signal_bot_e164 or ""
    # Format messages for the API, excluding the bot's own messages from evidence
    formatted_messages = []
    for m in messages:
        # Use the enriched text (may include OCR JSON appended by the attachment pipeline)
        text = m.get("enriched_body") or m.get("text") or m.get("body") or ""
        if not text:
            # Keep attachment-only messages if they have image_payloads
            if not m.get("image_payloads"):
                continue
        sender = m.get("sender") or m.get("source") or "unknown"
        if bot_e164 and sender == bot_e164:
            continue  # Don't include bot's own messages in case evidence
        sender_name = m.get("sender_name") or None
        ts = m.get("ts") or m.get("timestamp") or 0
        msg_id = m.get("id") or m.get("message_id") or str(ts)
        sender_hash = hashlib.sha256(sender.encode()).hexdigest()[:16]
        formatted_messages.append({
            "message_id": msg_id,
            "sender_hash": sender_hash,
            "sender_name": sender_name,
            "ts": ts,
            "content_text": text,
            "image_payloads": m.get("image_payloads") or [],
        })
    
    payload = {
        "token": token,
        "group_id": group_id,
        "cases": [{"case_block": b} for b in case_blocks],
        "messages": formatted_messages,  # Include raw messages for evidence linking
    }
    url = settings.signal_bot_url.rstrip("/") + "/history/cases"
    with httpx.Client(timeout=120) as client:  # Longer timeout for larger payloads
        r = client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()
        inserted = int(data.get("cases_inserted", 0))
        log.info("Posted %d cases + %d messages to signal-bot (%d inserted)", len(case_blocks), len(formatted_messages), inserted)
        return inserted


def _format_messages_for_bot(messages: List[dict], bot_e164: str = "") -> List[dict]:
    """Format messages for /history/cases API."""
    import hashlib
    formatted = []
    for m in messages:
        text = m.get("enriched_body") or m.get("text") or m.get("body") or ""
        if not text and not m.get("image_payloads"):
            continue
        sender = m.get("sender") or m.get("source") or "unknown"
        if bot_e164 and sender == bot_e164:
            continue
        sender_hash = hashlib.sha256(sender.encode()).hexdigest()[:16]
        formatted.append({
            "message_id": m.get("id") or m.get("message_id") or str(m.get("ts", 0)),
            "sender_hash": sender_hash,
            "sender_name": m.get("sender_name"),
            "ts": m.get("ts") or m.get("timestamp") or 0,
            "content_text": text,
            "image_payloads": m.get("image_payloads") or [],
        })
    return formatted


def _post_structured_cases_to_bot(
    *,
    settings,
    token: str,
    group_id: str,
    structured_cases: List[dict],
    messages: List[dict],
) -> int:
    """Post pre-structured cases (8x fewer API calls). Returns cases_inserted."""
    formatted_messages = _format_messages_for_bot(messages, bot_e164=settings.signal_bot_e164 or "")
    payload = {
        "token": token,
        "group_id": group_id,
        "cases_structured": [
            {
                "case_block": c["case_block"],
                "problem_title": c.get("problem_title", ""),
                "problem_summary": c.get("problem_summary", ""),
                "solution_summary": c.get("solution_summary", ""),
                "status": c.get("status", "solved"),
                "tags": c.get("tags") or [],
                "evidence_ids": c.get("evidence_ids") or [],
            }
            for c in structured_cases
        ],
        "messages": formatted_messages,
    }
    url = settings.signal_bot_url.rstrip("/") + "/history/cases"
    with httpx.Client(timeout=120) as client:
        r = client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()
        inserted = int(data.get("cases_inserted", 0))
        log.info("Posted %d structured cases + %d messages to signal-bot (%d inserted)", len(structured_cases), len(formatted_messages), inserted)
        return inserted


# ?????????????????????????????????????????????????????????????????????????????
# Main Job Handler - Signal Desktop with QR-based linking
# ?????????????????????????????????????????????????????????????????????????????

def _handle_history_link_desktop(*, settings, db, job_id: int, payload: Dict[str, Any]) -> None:
    """
    Handle HISTORY_LINK job using QR-based Signal Desktop linking.
    
    Flow:
    1. Reset Signal Desktop to show QR code
    2. Send QR code to user
    3. Wait for user to scan (poll until linked with user conversations)
    4. Read messages from group
    5. Extract cases with LLM
    6. Post cases to signal-bot
    """
    token = str(payload["token"])
    group_id = str(payload["group_id"])
    group_name = str(payload.get("group_name", ""))

    def check_cancelled():
        if is_job_cancelled(db, job_id=job_id):
            raise JobCancelled(f"Job {job_id} cancelled")

    try:
        check_cancelled()

        # Always reset and show QR for every group link (user requirement: ask for QR every time)
        # ?????????????????????????????????????????????????????????????????
        # Step 1: Reset Signal Desktop and get QR code
        # ?????????????????????????????????????????????????????????????????
        log.info("Resetting Signal Desktop for new user link...")
        _reset_desktop(settings)

        # Poll /status until Signal Desktop is unlinked AND DevTools is connected
        # (DevTools connected = Electron renderer is up = QR code is rendered on screen)
        log.info("Waiting for Signal Desktop to show QR code (polling status)...")
        qr_image = b""
        for attempt in range(40):  # up to 120 seconds
            time.sleep(3)
            try:
                status = _check_desktop_status(settings)
                is_unlinked = not status.get("linked", True)
                devtools_ready = status.get("devtools_connected", False)
                log.info(
                    "Desktop status: linked=%s devtools=%s (%d/40)",
                    status.get("linked"), devtools_ready, attempt + 1,
                )
                if is_unlinked and devtools_ready:
                    log.info("Signal Desktop is unlinked and DevTools ready — QR visible, taking screenshot")
                    # Wait for QR to fully render; retry up to 3 times if screenshot is blank
                    for sc_attempt in range(3):
                        time.sleep(5)
                        qr_image = _get_desktop_screenshot(settings)
                        log.info("Screenshot attempt %d size: %d bytes", sc_attempt + 1, len(qr_image))
                        if len(qr_image) > 2000:  # valid QR is at least a few KB
                            break
                        log.info("Screenshot looks blank, waiting longer...")
                    break
                elif is_unlinked:
                    log.info("Unlinked but DevTools not ready yet, waiting...")
            except Exception as e:
                log.info("Status not ready yet: %s (%d/40)", e, attempt + 1)

        if not qr_image:
            log.error("Signal Desktop never showed QR after 72s")
            _notify_link_result(
                settings=settings,
                token=token,
                success=False,
                note="Failed to get QR code. Signal Desktop may not be ready.",
            )
            return

        # Send QR to user
        if not _send_qr_to_user(settings=settings, token=token, qr_image=qr_image):
            _notify_link_result(
                settings=settings,
                token=token,
                success=False,
                note="Failed to send QR code to user.",
            )
            return

        _notify_progress(settings=settings, token=token, progress_key="qr_sent")

        # Step 2: Wait for user to scan QR code (QR expires after ~5 minutes)
        log.info("Waiting for user to scan QR code...")
        max_wait_seconds = 270  # slightly under the ~5-min QR expiry so we detect it in time
        poll_interval = 3
        waited = 0

        while waited < max_wait_seconds:
            check_cancelled()
            time.sleep(poll_interval)
            waited += poll_interval

            status = _check_desktop_status(settings)
            if status.get("has_user_conversations"):
                log.info("User linked! Found %d conversations", status.get("conversations_count", 0))
                break
        else:
            log.warning("QR code expired without a successful scan")
            _notify_link_result(
                settings=settings,
                token=token,
                success=False,
                note="QR code expired. Please start the import again to get a fresh code.",
            )
            return

        log.info("Signal Desktop is linked, waiting for sync then verifying group: %s", group_name or group_id)

        # Security: Verify the linked admin account is in the target group.
        # This check is independent from the bot-side check in /history/cases.
        # Defense-in-depth: BOTH the admin and the bot must be in the group.
        #
        # After QR scan Signal Desktop needs time to sync conversations from the
        # server (typically 10-30s). We poll until the group appears or timeout.
        convs_url = settings.signal_desktop_url.rstrip("/") + "/conversations"
        group_name_lower = group_name.lower().strip() if group_name else ""
        sync_timeout = 60  # seconds to wait for group to appear after linking
        sync_poll = 5
        sync_waited = 0
        admin_in_group = False

        def _check_group_in_convs(convs_data: list) -> bool:
            for conv in convs_data:
                if conv.get("type") != "group":
                    continue
                if conv.get("groupId") == group_id or conv.get("id") == group_id:
                    return True
                if group_name_lower and (conv.get("name") or "").lower().strip() == group_name_lower:
                    return True
            return False

        try:
            while sync_waited <= sync_timeout:
                check_cancelled()
                with httpx.Client(timeout=30) as client:
                    resp = client.get(convs_url)
                    resp.raise_for_status()
                    convs_data = resp.json().get("conversations", [])

                if _check_group_in_convs(convs_data):
                    admin_in_group = True
                    log.info(
                        "Admin verified in group '%s' after %ds sync (%d conversations)",
                        group_name, sync_waited, len(convs_data)
                    )
                    break

                if sync_waited == 0:
                    log.info(
                        "Group '%s' not yet in admin's %d conversations ? waiting for sync...",
                        group_name, len(convs_data)
                    )
                    _notify_progress(settings=settings, token=token, progress_key="syncing")
                sync_waited += sync_poll
                if sync_waited <= sync_timeout:
                    time.sleep(sync_poll)

            if not admin_in_group:
                log.warning(
                    "SECURITY BLOCK: Admin is NOT in group '%s' (id=%s...) after %ds sync. "
                    "Admin has %d conversations.",
                    group_name, group_id[:20], sync_timeout, len(convs_data)
                )
                _notify_link_result(
                    settings=settings,
                    token=token,
                    success=False,
                    note=(
                        f"Your Signal account is not in group '{group_name}'. "
                        "Both you and the bot must be members to import history."
                    ),
                )
                return
        except JobCancelled:
            raise
        except Exception as e:
            log.error("Could not verify admin group membership ? blocking: %s", e)
            _notify_link_result(
                settings=settings,
                token=token,
                success=False,
                note="Could not verify group membership. Please try again.",
            )
            return

        # ────────────────────────────────────────────────────────────────────────
        # Step 3: Download group attachments directly from Signal's CDN
        #
        # Signal Desktop stores cdnKey + encryption key in its SQLite DB for
        # every attachment, even ones it hasn't downloaded yet.  We use these to
        # fetch and decrypt files ourselves, with no CDP or JS involved.
        # ────────────────────────────────────────────────────────────────────────
        check_cancelled()
        log.info("Downloading group attachments from Signal CDN for group %s...", group_name or group_id)
        _notify_progress(settings=settings, token=token, progress_key="syncing")
        _fetch_attachments_direct(settings, group_id=group_id, group_name=group_name)
        check_cancelled()

        # ?????????????????????????????????????????????????????????????????
        # Step 4: Fetch messages from Signal Desktop
        # ?????????????????????????????????????????????????????????????????
        _notify_progress(settings=settings, token=token, progress_key="collecting")

        msgs = _get_desktop_messages(settings, group_id=group_id, group_name=group_name)
        
        if not msgs:
            log.warning("No messages found for group: %s", group_name or group_id)
            _notify_link_result(
                settings=settings,
                token=token,
                success=True,
                message_count=0,
                cases_found=0,
                note="No messages found in group. The group may not exist or has no messages.",
            )
            return

        log.info("Fetched %d messages from Signal Desktop", len(msgs))
        _notify_progress(settings=settings, token=token, progress_key="found_messages", count=len(msgs))

        check_cancelled()

        # ?????????????????????????????????????????????????????????????????
        # Step 4b: Enrich messages that have attachments with OCR text
        # Each message dict gets an "enriched_body" and "image_payloads" key.
        # ?????????????????????????????????????????????????????????????????
        openai_client_early = OpenAI(
            api_key=settings.openai_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
        msgs = _enrich_messages_with_attachments(
            settings=settings,
            openai_client=openai_client_early,
            messages=msgs,
        )

        check_cancelled()

        # ?????????????????????????????????????????????????????????????????
        # Step 5: Process messages - extract structured cases (8x fewer API calls)
        # ?????????????????????????????????????????????????????????????????
        chunks = _chunk_messages(
            messages=msgs,
            max_chars=settings.chunk_max_chars,
            overlap_messages=settings.chunk_overlap_messages,
            bot_e164=settings.signal_bot_e164 or "",
        )
        log.info("Split into %d chunks for processing", len(chunks))

        all_structured: List[dict] = []
        for i, ch in enumerate(chunks):
            check_cancelled()
            if len(chunks) > 1:
                _notify_progress(settings=settings, token=token, progress_key="processing_chunk", current=i+1, total=len(chunks))
            all_structured.extend(
                _extract_structured_cases(
                    openai_client=openai_client_early,
                    model=settings.model_blocks,
                    fallback_models=settings.model_blocks_fallback,
                    chunk_text=ch,
                )
            )

        deduped = _dedup_cases_llm(
            openai_client=openai_client_early,
            model=settings.model_blocks,
            fallback_models=settings.model_blocks_fallback,
            cases=all_structured,
        )

        # ?????????????????????????????????????????????????????????????????
        # Step 6: Post structured cases to signal-bot (batch embed on bot side)
        # ?????????????????????????????????????????????????????????????????
        cases_inserted = 0
        if deduped:
            _notify_progress(settings=settings, token=token, progress_key="saving_cases", count=len(deduped))
            cases_inserted = _post_structured_cases_to_bot(
                settings=settings, token=token, group_id=group_id,
                structured_cases=deduped, messages=msgs,
            )
        else:
            log.info("No solved cases found in messages")

        _notify_link_result(
            settings=settings,
            token=token,
            success=True,
            message_count=len(msgs),
            cases_found=len(deduped),
            cases_inserted=cases_inserted,
        )
        
        # SECURITY: Reset Signal Desktop session after successful ingest
        # This ensures user's account is unlinked and requires new QR scan next time
        log.info("Resetting Signal Desktop session for security (unlinking user account)...")
        try:
            _reset_desktop(settings)
            log.info("Signal Desktop session reset successfully")
        except Exception as e:
            log.warning("Failed to reset Signal Desktop session: %s", e)

    except JobCancelled:
        log.info("Job %d was cancelled", job_id)
        # Also reset on cancellation for security
        try:
            _reset_desktop(settings)
        except Exception:
            pass
        raise


# ?????????????????????????????????????????????????????????????????????????????
# Main Loop
# ?????????????????????????????????????????????????????????????????????????????

def main() -> None:
    _configure_logging()
    settings = load_settings()
    db = create_db(settings)

    log.info("signal-ingest started (poll=%.2fs)", settings.worker_poll_seconds)
    
    if settings.use_signal_desktop:
        log.info("Mode: Signal Desktop (using already-linked instance at %s)", settings.signal_desktop_url)
    else:
        log.warning("Signal Desktop not enabled - history sync will not work")

    while True:
        job = claim_next_job(db, allowed_types=[HISTORY_LINK, HISTORY_SYNC])
        if job is None:
            time.sleep(settings.worker_poll_seconds)
            continue

        try:
            if job.type == HISTORY_LINK:
                if settings.use_signal_desktop:
                    _handle_history_link_desktop(settings=settings, db=db, job_id=job.job_id, payload=job.payload)
                else:
                    log.error("HISTORY_LINK job received but Signal Desktop is not enabled")
                    _notify_link_result(
                        settings=settings,
                        token=job.payload.get("token", ""),
                        success=False,
                        note="Signal Desktop is not enabled on server.",
                    )
            else:
                raise RuntimeError(f"Unknown job type: {job.type}")

            complete_job(db, job_id=job.job_id)

        except JobCancelled:
            log.info("Job cancelled: id=%s", job.job_id)
        except Exception:
            log.exception("Job failed: id=%s type=%s", job.job_id, job.type)
            _notify_link_result(
                settings=settings,
                token=job.payload.get("token", ""),
                success=False,
            )
            fail_job(db, job_id=job.job_id, attempts=job.attempts)


if __name__ == "__main__":
    main()
