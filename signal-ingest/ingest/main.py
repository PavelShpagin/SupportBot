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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from openai import OpenAI

from ingest.config import load_settings
from ingest.db import claim_next_job, complete_job, create_db, fail_job, is_job_cancelled

HISTORY_LINK = "HISTORY_LINK"
HISTORY_SYNC = "HISTORY_SYNC"


def _extract_video_thumbnail(video_bytes: bytes) -> bytes | None:
    """Extract a single thumbnail frame from in-memory video bytes using OpenCV.

    Picks a frame at ~1 second (or the first frame for very short clips).
    Returns JPEG bytes or None on failure.
    """
    import tempfile
    import os
    try:
        import cv2
    except ImportError:
        return None
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp.write(video_bytes)
            tmp_path = tmp.name
        cap = cv2.VideoCapture(tmp_path)
        if not cap.isOpened():
            return None
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        target = min(int(fps), max(total - 1, 0))
        cap.set(cv2.CAP_PROP_POS_FRAMES, target)
        ret, frame = cap.read()
        cap.release()
        if not ret or frame is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return buf.tobytes() if ok else None
    except Exception:
        return None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def _extract_video_audio_from_bytes(video_bytes: bytes) -> tuple[bytes, str] | None:
    """Extract audio from in-memory video bytes using ffmpeg.

    Uses stream copy (no re-encoding) for speed. Falls back to mp3 encoding
    if copy fails. Returns (audio_bytes, mime_type) or None.
    """
    import subprocess
    tmp_video = None
    tmp_audio = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp.write(video_bytes)
            tmp_video = tmp.name
        # First try: copy audio stream as-is (near-instant)
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as tmp:
            tmp_audio = tmp.name
        result = subprocess.run(
            [
                "ffmpeg", "-i", tmp_video,
                "-vn", "-acodec", "copy",
                "-y", tmp_audio,
            ],
            capture_output=True,
            timeout=30,
        )
        mime = "audio/mp4"
        # Fallback: re-encode to mp3 if copy failed
        if result.returncode != 0:
            os.unlink(tmp_audio)
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                tmp_audio = tmp.name
            result = subprocess.run(
                [
                    "ffmpeg", "-i", tmp_video,
                    "-vn", "-acodec", "libmp3lame",
                    "-ar", "16000", "-ac", "1", "-q:a", "9",
                    "-y", tmp_audio,
                ],
                capture_output=True,
                timeout=60,
            )
            mime = "audio/mp3"
        if result.returncode != 0:
            return None
        audio_bytes = Path(tmp_audio).read_bytes()
        if len(audio_bytes) < 1000:
            return None
        return audio_bytes, mime
    except Exception:
        return None
    finally:
        for p in (tmp_video, tmp_audio):
            if p:
                try:
                    os.unlink(p)
                except Exception:
                    pass


def _transcribe_audio_bytes(
    audio_bytes: bytes,
    openai_client: "OpenAI" = None,  # kept for API compatibility, unused
    context: str = "",
    mime_type: str = "audio/mp4",
) -> str:
    """Transcribe audio using the native Gemini SDK."""
    import os
    import google.generativeai as genai

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        log.warning("GOOGLE_API_KEY not set, cannot transcribe audio")
        return ""
    genai.configure(api_key=api_key)
    prompt = (
        "Transcribe this audio verbatim. Return ONLY the spoken words, "
        "no timestamps or annotations. If there is no speech or only "
        "noise/music, return exactly: EMPTY"
    )
    if context:
        prompt += f"\nContext: {context}"
    try:
        log.info("Sending %d bytes of audio (%s) to Gemini for transcription", len(audio_bytes), mime_type)
        model = genai.GenerativeModel("gemini-2.5-flash")
        response = model.generate_content([
            prompt,
            {"mime_type": mime_type, "data": audio_bytes},
        ])
        text = (response.text or "").strip()
        if text == "EMPTY" or not text:
            log.info("Audio transcription returned empty/EMPTY")
            return ""
        log.info("Audio transcription result: %s", text[:200])
        return text
    except Exception as e:
        log.warning("Audio transcription failed: %s", e)
        return ""


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

Images may be attached alongside the text. Use them to better understand the problem and solution context.

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
- problem_title MUST accurately describe the problem_summary content. Do NOT use titles from other conversations in the chunk.
- problem_title, problem_summary, solution_summary in Ukrainian.
- evidence_ids: extract ALL msg_id=XXX from headers.
- Each case must be self-consistent: the title, problem, and solution must all describe the SAME issue.
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


def _get_clean_qr(settings) -> bytes:
    """Get a cleanly regenerated QR PNG from Signal Desktop's /qr-png endpoint.

    Returns the PNG bytes, or empty bytes on failure (caller should fall back to screenshot).
    """
    url = settings.signal_desktop_url.rstrip("/") + "/qr-png"
    try:
        with httpx.Client(timeout=30) as client:
            r = client.get(url)
            r.raise_for_status()
            return r.content
    except Exception as e:
        log.warning("Clean QR (/qr-png) failed, will fall back to screenshot: %s", e)
        return b""


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
    max_bytes: int = 20_000_000,
    retries: int = 3,
    retry_delay: float = 2.0,
) -> Optional[bytes]:
    """Fetch raw bytes for a Signal Desktop attachment with retries.

    Tries two sources, on-disk path first (unique per attachment), then CDN:
    1. ``GET /attachment?path=...`` — on-disk (auto-decrypts v2 encrypted files).
    2. ``GET /attachment/by-cdn/{cdn_key}`` — CDN-downloaded + decrypted cache.

    On-disk is preferred because each attachment has a unique path, while
    multiple attachments can share the same cdnKey (Signal reuses CDN upload
    slots), which would return the same cached blob for all of them.

    Retries each source on transient failures (timeouts, 5xx).
    Returns None if all attempts fail or the file is too large.
    """
    base = settings.signal_desktop_url.rstrip("/")

    def _get_with_retry(url: str, *, attempts: int = retries, **kw) -> Optional[bytes]:
        delay = retry_delay
        for attempt in range(1, attempts + 1):
            try:
                with httpx.Client(timeout=60) as client:
                    r = client.get(url, **kw)
                    if r.status_code == 404:
                        return None
                    r.raise_for_status()
                    if len(r.content) > max_bytes:
                        log.warning("Attachment too large (%d bytes), skipping", len(r.content))
                        return None
                    return r.content
            except httpx.HTTPStatusError as e:
                if e.response.status_code < 500:
                    return None
                if attempt < attempts:
                    log.warning("Fetch %s attempt %d/%d got %d, retrying in %.1fs",
                                url.split("/")[-1][:30], attempt, attempts,
                                e.response.status_code, delay)
                    time.sleep(delay)
                    delay *= 2
                else:
                    log.warning("Fetch %s failed after %d attempts: %s", url.split("/")[-1][:30], attempts, e)
                    return None
            except Exception as e:
                if attempt < attempts:
                    log.warning("Fetch %s attempt %d/%d failed: %s, retrying in %.1fs",
                                url.split("/")[-1][:30], attempt, attempts, e, delay)
                    time.sleep(delay)
                    delay *= 2
                else:
                    log.warning("Fetch %s failed after %d attempts: %s", url.split("/")[-1][:30], attempts, e)
                    return None
        return None

    # On-disk path first — each attachment has a unique path even when
    # multiple attachments share the same cdnKey (Signal reuses upload
    # slots).  The on-disk endpoint handles v2 decryption transparently.
    if rel_path:
        data = _get_with_retry(f"{base}/attachment", params={"path": rel_path})
        if data is not None:
            return data

    # CDN cache fallback — useful when path is missing (CDN-only attachments)
    if cdn_key:
        data = _get_with_retry(f"{base}/attachment/by-cdn/{cdn_key}")
        if data is not None:
            return data

    if rel_path or cdn_key:
        log.warning(
            "Attachment not found via cdn_key=%s path=%r (after %d retries each)",
            cdn_key or "(none)", rel_path or "(none)", retries,
        )
    return None


def _ocr_attachment(
    openai_client: OpenAI,
    model: str,
    image_bytes: bytes,
    content_type: str,
    context_text: str = "",
    fallback_models: list | None = None,
    timeout: float = 45.0,
) -> str:
    """Run multimodal OCR on an image and return a structured description.

    Extracts all visible text verbatim and a concise functional description
    of what the image shows in the context of the support conversation.
    Returns an empty string on failure (the message is still stored without OCR).

    Uses _llm_call_with_fallback for timeout and cascade protection.
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
        resp = _llm_call_with_fallback(
            openai_client=openai_client,
            model=model,
            fallback_models=fallback_models or [],
            timeout=timeout,
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
    timeout: int = 180,
    retries: int = 3,
) -> dict:
    """Download all pending group attachments from Signal's CDN or decrypt
    v2 locally-encrypted files.  Retries on failure.

    Calls ``POST /attachments/fetch-all`` on signal-desktop, which reads
    attachment CDN metadata from the SQLite DB, downloads each encrypted blob
    from ``cdn{N}.signal.org``, decrypts it (AES-256-CBC + HMAC-SHA256), and
    caches the plaintext at ``{signal_data_dir}/cdn-cache/{cdnKey}``.
    Also decrypts v2 on-disk encrypted files using ``localKey``.

    Returns the response dict: ``{downloaded, decrypted_local, failed, skipped}``.
    """
    url = settings.signal_desktop_url.rstrip("/") + "/attachments/fetch-all"
    params = {"group_id": group_id, "group_name": group_name}
    delay = 3.0
    for attempt in range(1, retries + 1):
        try:
            with httpx.Client(timeout=timeout) as client:
                r = client.post(url, params=params)
                r.raise_for_status()
                result = r.json()
                log.info(
                    "Direct CDN fetch (attempt %d): downloaded=%s decrypted_local=%s failed=%s skipped=%s",
                    attempt, result.get("downloaded"), result.get("decrypted_local"),
                    result.get("failed"), result.get("skipped"),
                )
                return result
        except Exception as e:
            if attempt < retries:
                log.warning("Direct CDN fetch attempt %d/%d failed: %s — retrying in %.1fs", attempt, retries, e, delay)
                time.sleep(delay)
                delay *= 2
            else:
                log.warning("Direct CDN fetch failed after %d attempts (non-fatal): %s", retries, e)
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


def _format_ocr_for_video(ocr_text: str) -> str:
    """Parse raw JSON OCR result into human-readable text for video thumbnails.

    _ocr_attachment() returns raw JSON like {"extracted_text": "...", "description": "..."}.
    This formats it into a clean string like "Текст: ... | Опис: ..." for display.
    """
    try:
        parsed = _safe_json_loads(ocr_text)
        parts = []
        et = (parsed.get("extracted_text") or "").strip()
        desc = (parsed.get("description") or "").strip()
        if et:
            parts.append(f"Текст: {et}")
        if desc:
            parts.append(f"Опис: {desc}")
        return " | ".join(parts) if parts else ocr_text
    except Exception:
        return ocr_text


def _enrich_messages_with_attachments(
    *,
    settings,
    openai_client: OpenAI,
    messages: List[dict],
    max_att_per_message: int = 3,
    max_ocr_workers: int = 6,
) -> List[dict]:
    """Fetch attachment bytes for each message, OCR them, and enrich the body.

    Each message dict is copied with two new keys:
    - ``enriched_body``: original body + appended OCR JSON for each attachment.
    - ``image_payloads``: list of ``{filename, content_type, data_b64}`` dicts
      for storage in signal-bot's raw_messages.image_paths_json.

    Messages without attachments pass through unchanged (but gain empty keys).
    OCR calls run in parallel (up to max_ocr_workers) to avoid serial bottleneck.
    """
    msgs_with_atts = [m for m in messages if m.get("attachments")]
    total_img = sum(
        1 for m in messages for a in (m.get("attachments") or [])
        if isinstance(a, dict) and (a.get("contentType") or "").startswith("image/")
    )
    log.info(
        "Enriching %d messages — %d have attachments, %d image(s) to fetch+OCR",
        len(messages), len(msgs_with_atts), total_img,
    )

    # Phase 1: fetch all attachment bytes (I/O-bound, parallelise)
    # Build work items: (msg_index, att_index, att_metadata)
    fetch_tasks = []
    for mi, m in enumerate(messages):
        for ai, att in enumerate((m.get("attachments") or [])[:max_att_per_message]):
            rel_path = att.get("path") or ""
            cdn_key = att.get("cdnKey") or ""
            if rel_path or cdn_key:
                fetch_tasks.append((mi, ai, att, rel_path, cdn_key))

    # Parallel fetch
    fetched: Dict[tuple, bytes] = {}
    if fetch_tasks:
        def _do_fetch(task):
            mi, ai, att, rel_path, cdn_key = task
            data = _fetch_attachment(settings, rel_path, cdn_key=cdn_key)
            return (mi, ai), data

        with ThreadPoolExecutor(max_workers=max_ocr_workers) as pool:
            for (key, data) in pool.map(lambda t: _do_fetch(t), fetch_tasks):
                if data is not None:
                    fetched[key] = data

    # Phase 2: OCR images (and video thumbnails) + video transcripts in parallel
    video_thumbs: Dict[tuple, bytes] = {}
    video_transcripts: Dict[tuple, str] = {}
    ocr_tasks = []
    transcript_tasks = []
    for (mi, ai), data in fetched.items():
        att = messages[mi].get("attachments", [])[ai]
        ct = att.get("contentType") or "application/octet-stream"
        if ct.startswith("image/"):
            body = messages[mi].get("body") or messages[mi].get("text") or ""
            ocr_tasks.append((mi, ai, data, ct, body))
        elif ct.startswith("video/"):
            thumb = _extract_video_thumbnail(data)
            if thumb:
                video_thumbs[(mi, ai)] = thumb
                body = messages[mi].get("body") or messages[mi].get("text") or ""
                fname = att.get("fileName") or "video"
                ocr_tasks.append((mi, ai, thumb, "image/jpeg", f"Video thumbnail from: {fname}\n{body}"))
            audio_result = _extract_video_audio_from_bytes(data)
            if audio_result:
                audio, audio_mime = audio_result
                body = messages[mi].get("body") or messages[mi].get("text") or ""
                transcript_tasks.append((mi, ai, audio, audio_mime, body))

    ocr_results: Dict[tuple, str] = {}
    if ocr_tasks:
        log.info("Running OCR on %d images/video-thumbs in parallel (workers=%d)", len(ocr_tasks), min(len(ocr_tasks), max_ocr_workers))

        def _do_ocr(task):
            mi, ai, img_bytes, ct, body = task
            result = _ocr_attachment(
                openai_client=openai_client,
                model=settings.model_img,
                image_bytes=img_bytes,
                content_type=ct,
                context_text=body,
                fallback_models=settings.model_img_fallback,
            )
            return (mi, ai), result

        with ThreadPoolExecutor(max_workers=max_ocr_workers) as pool:
            for (key, result) in pool.map(lambda t: _do_ocr(t), ocr_tasks):
                if result:
                    ocr_results[key] = result

    if transcript_tasks:
        log.info("Transcribing %d video audio tracks in parallel", len(transcript_tasks))

        def _do_transcribe(task):
            mi, ai, audio, audio_mime, body = task
            text = _transcribe_audio_bytes(audio, openai_client=openai_client, context=body, mime_type=audio_mime)
            return (mi, ai), text

        with ThreadPoolExecutor(max_workers=min(len(transcript_tasks), max_ocr_workers)) as pool:
            for (key, text) in pool.map(lambda t: _do_transcribe(t), transcript_tasks):
                if text:
                    video_transcripts[key] = text

    # Phase 3: assemble enriched messages
    enriched: List[dict] = []
    att_count = 0
    for mi, m in enumerate(messages):
        atts = m.get("attachments") or []
        if not atts:
            enriched.append({**m, "enriched_body": m.get("body") or m.get("text") or "", "image_payloads": []})
            continue

        body = m.get("body") or m.get("text") or ""
        payloads: List[dict] = []
        ocr_texts: List[str] = []

        for ai, att in enumerate(atts[:max_att_per_message]):
            data = fetched.get((mi, ai))
            if data is None:
                continue
            att_count += 1
            content_type = att.get("contentType") or "application/octet-stream"

            if content_type.startswith("image/"):
                ocr_text = ocr_results.get((mi, ai))
                if ocr_text:
                    ocr_texts.append(ocr_text)
            elif content_type.startswith("video/"):
                thumb = video_thumbs.get((mi, ai))
                if thumb:
                    fname = att.get("fileName") or "video"
                    ocr_text = ocr_results.get((mi, ai))
                    desc = _format_ocr_for_video(ocr_text) if ocr_text else ""
                    ocr_texts.append(f"[Відео: {fname}" + (f" — {desc}" if desc else "") + "]")
                    payloads.append({
                        "filename": fname + "_thumb.jpg",
                        "content_type": "image/jpeg",
                        "data_b64": base64.b64encode(thumb).decode("utf-8"),
                    })
                else:
                    fname = att.get("fileName") or (att.get("path") or "").split("/")[-1] or "video"
                    ocr_texts.append(f'[Відео: {fname} ({content_type})]')
                transcript = video_transcripts.get((mi, ai))
                if transcript:
                    ocr_texts.append(f"[Транскрипт відео: {transcript}]")
                continue
            else:
                fname = att.get("fileName") or (att.get("path") or "").split("/")[-1] or "attachment"
                ocr_texts.append(f'[attachment: {fname} ({content_type})]')

            payloads.append({
                "filename": att.get("fileName") or "",
                "content_type": content_type,
                "data_b64": base64.b64encode(data).decode("utf-8"),
            })

        enriched_body = body
        if ocr_texts:
            enriched_body = body + "\n\n" + "\n".join(f"[image]\n{t}" for t in ocr_texts)

        enriched.append({**m, "enriched_body": enriched_body, "image_payloads": payloads})

    if att_count:
        log.info("Processed %d attachments across %d messages", att_count, len(messages))
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
                    bot_e164: str = "") -> List[tuple[str, list[tuple[bytes, str]]]]:
    """Split messages into chunks for LLM processing. Bot messages are excluded.

    Returns list of (chunk_text, chunk_images) tuples. Images are interleaved
    with text via [[IMG:N]] markers so the LLM sees each image at its natural
    position in the conversation.
    """
    formatted: list[tuple[str, list[dict]]] = []
    for m in messages:
        text = m.get("enriched_body") or m.get("text") or m.get("body") or ""
        if not text:
            continue
        sender = m.get("sender") or m.get("source") or "unknown"
        if _is_bot_message(text, sender, bot_e164):
            continue
        ts = m.get("ts") or m.get("timestamp") or 0
        msg_id = m.get("id") or m.get("message_id") or str(ts)
        reactions = int(m.get("reactions") or 0)
        header = f'{sender} ts={ts} msg_id={msg_id}'
        quote_id = m.get("quote_id") or m.get("reply_to_id")
        if quote_id:
            header += f' reply_to={quote_id}'
        if reactions > 0:
            header += f' reactions={reactions}'
            rxn_emoji = m.get("reaction_emoji") or ""
            if rxn_emoji:
                header += f' reaction_emoji={rxn_emoji}'
        payloads = m.get("image_payloads") or []
        formatted.append((f'{header}\n{text}\n', payloads))

    chunks: list[tuple[str, list[tuple[bytes, str]]]] = []
    cur_texts: list[str] = []
    cur_payloads: list[list[dict]] = []

    for line_text, line_payloads in formatted:
        candidate = "".join(cur_texts) + line_text
        if len(candidate) > max_chars and cur_texts:
            chunk_text, chunk_imgs = _build_interleaved_chunk(cur_texts, cur_payloads)
            chunks.append((chunk_text, chunk_imgs))
            cur_texts = cur_texts[-overlap_messages:] if overlap_messages > 0 else []
            cur_payloads = cur_payloads[-overlap_messages:] if overlap_messages > 0 else []
        cur_texts.append(line_text)
        cur_payloads.append(line_payloads)

    if cur_texts:
        chunk_text, chunk_imgs = _build_interleaved_chunk(cur_texts, cur_payloads)
        chunks.append((chunk_text, chunk_imgs))

    return chunks


def _build_interleaved_chunk(
    texts: list[str], payloads_per_msg: list[list[dict]], max_images: int = 5,
) -> tuple[str, list[tuple[bytes, str]]]:
    """Build chunk text with [[IMG:N]] markers and collect images in order."""
    images: list[tuple[bytes, str]] = []
    result_texts: list[str] = []
    img_idx = 0

    for msg_text, msg_payloads in zip(texts, payloads_per_msg):
        msg_images: list[tuple[bytes, str]] = []
        for p in msg_payloads:
            ct = p.get("content_type") or ""
            if not ct.startswith("image/"):
                continue
            try:
                raw = base64.b64decode(p["data_b64"])
                msg_images.append((raw, ct))
            except Exception:
                continue
            if img_idx + len(msg_images) >= max_images:
                break

        if msg_images and img_idx < max_images:
            markers = " ".join(f"[[IMG:{img_idx + j}]]" for j in range(len(msg_images)))
            result_texts.append(msg_text.rstrip("\n") + f"\n{markers}\n")
            images.extend(msg_images)
            img_idx += len(msg_images)
        else:
            result_texts.append(msg_text)

    return "".join(result_texts), images



def _llm_call_with_fallback(
    *,
    openai_client: OpenAI,
    model: str,
    fallback_models: list,
    timeout: float,
    **kwargs,
):
    """Call openai_client.chat.completions.create with fast model cascade.

    Tries `model` first, then each entry in `fallback_models` in order.
    Falls back on transient errors (404, 429, 499, 503, timeout).
    Max ~10s spent per model (1 retry with 2s backoff) before cascading.
    Raises the last exception if all models are exhausted.
    """
    import openai as _openai

    models_to_try = [model] + list(fallback_models)
    last_exc: Exception | None = None
    for i, m in enumerate(models_to_try):
        is_last = (i == len(models_to_try) - 1)
        # Last model gets more patience
        model_timeout = timeout * 1.5 if is_last and len(models_to_try) > 1 else timeout
        max_attempts = 2 if is_last else 1  # only retry on last model

        for attempt in range(max_attempts):
            t0 = time.time()
            try:
                result = openai_client.chat.completions.create(model=m, timeout=model_timeout, **kwargs)
                log.info("LLM call model=%s completed in %.1fs", m, time.time() - t0)
                return result
            except (_openai.APITimeoutError, _openai.APIStatusError) as e:
                elapsed = time.time() - t0
                status_code = getattr(e, "status_code", None)
                is_retryable = isinstance(e, _openai.APITimeoutError) or status_code in (404, 429, 499, 503)
                if not is_retryable:
                    raise
                last_exc = e
                # Last model, can retry once with short backoff
                if is_last and attempt < max_attempts - 1:
                    log.warning("Model %s failed after %.1fs (status=%s), retrying in 2s...", m, elapsed, status_code)
                    time.sleep(2)
                    continue
                # Cascade immediately to next model
                log.warning(
                    "Model %s failed after %.1fs (%s status=%s), cascading...",
                    m, elapsed, type(e).__name__, status_code,
                )
                last_exc = e
                time.sleep(1)
                break  # move to next model
    raise last_exc


def _extract_case_blocks(
    *,
    openai_client: OpenAI,
    model: str,
    chunk_text: str,
    timeout: float = 60.0,
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
    images: list[tuple[bytes, str]] | None = None,
    timeout: float = 120.0,
) -> List[dict]:
    """Extract solved support cases with full structured fields in one pass.

    Images are interleaved via [[IMG:N]] markers already present in chunk_text.
    """
    if images:
        # Build interleaved parts: split text on [[IMG:N]] markers
        import re
        marker_re = re.compile(r"\[\[IMG:(\d+)\]\]")
        segments = marker_re.split(f"HISTORY_CHUNK:\n{chunk_text}")
        user_parts: list = []
        referenced: set[int] = set()
        for i, seg in enumerate(segments):
            if i % 2 == 0:
                if seg:
                    user_parts.append({"type": "text", "text": seg})
            else:
                idx = int(seg)
                referenced.add(idx)
                if idx < len(images):
                    img_bytes, img_mime = images[idx]
                    b64 = base64.b64encode(img_bytes).decode("ascii")
                    user_parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{img_mime};base64,{b64}"},
                    })
        # Append unreferenced images at end
        for idx, (img_bytes, img_mime) in enumerate(images):
            if idx not in referenced:
                b64 = base64.b64encode(img_bytes).decode("ascii")
                user_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{img_mime};base64,{b64}"},
                })
    else:
        user_parts = None

    resp = _llm_call_with_fallback(
        openai_client=openai_client,
        model=model,
        fallback_models=fallback_models,
        timeout=timeout,
        messages=[
            {"role": "system", "content": P_BLOCKS_STRUCTURED},
            {"role": "user", "content": user_parts if user_parts else f"HISTORY_CHUNK:\n{chunk_text}"},
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
            "reply_to_id": m.get("quote_id") or m.get("reply_to_id"),
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
            "reply_to_id": m.get("quote_id") or m.get("reply_to_id"),
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
        # ─────────────────────────────────────────────────────────────
        # Step 1: Reset Signal Desktop and get QR code
        # ─────────────────────────────────────────────────────────────
        log.info("Resetting Signal Desktop for new user link...")
        _reset_desktop(settings)

        # Give Signal Desktop extra time for cold boot before polling
        time.sleep(10)

        # Poll /status until Signal Desktop is unlinked AND DevTools is connected
        log.info("Waiting for Signal Desktop to show QR code (polling status)...")
        qr_image = b""
        for attempt in range(50):  # up to 150 seconds
            time.sleep(3)
            try:
                status = _check_desktop_status(settings)
                is_unlinked = not status.get("linked", True)
                devtools_ready = status.get("devtools_connected", False)
                log.info(
                    "Desktop status: linked=%s devtools=%s (%d/50)",
                    status.get("linked"), devtools_ready, attempt + 1,
                )
                if is_unlinked and devtools_ready:
                    log.info("Signal Desktop is unlinked and DevTools ready — fetching QR")
                    time.sleep(5)  # let QR fully render
                    # Try clean QR generation first (extracts URL from DOM, regenerates QR)
                    qr_image = _get_clean_qr(settings)
                    if qr_image:
                        log.info("Got clean QR via /qr-png: %d bytes", len(qr_image))
                    else:
                        # Fallback to screenshot with retries
                        for sc_attempt in range(5):
                            time.sleep(5)
                            qr_image = _get_desktop_screenshot(settings)
                            log.info("Screenshot attempt %d size: %d bytes", sc_attempt + 1, len(qr_image))
                            if len(qr_image) > 5000:
                                break
                            log.info("Screenshot looks blank, waiting longer...")
                    break
                elif is_unlinked:
                    log.info("Unlinked but DevTools not ready yet, waiting...")
            except Exception as e:
                log.info("Status not ready yet: %s (%d/50)", e, attempt + 1)

        if not qr_image:
            log.error("Signal Desktop never showed QR after ~160s")
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

        # Step 2: Wait for user to scan QR code (QR expires after ~10 minutes)
        log.info("Waiting for user to scan QR code...")
        max_wait_seconds = 570
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
        # Step 3: Wait for Signal Desktop to download group attachments
        #
        # Signal Desktop downloads attachment files in the background after linking.
        # CDP trigger is attempted as an optimization to prioritise this group but
        # is NOT required — Signal Desktop downloads regardless.
        # We poll /attachments/status specifically for this group until pending == 0
        # (all attachment path fields written to DB by Signal Desktop) or timeout.
        # Direct CDN fetch handles attachments that already have cdnKey+key in DB
        # (messages received while this device was linked).
        # ────────────────────────────────────────────────────────────────────────
        check_cancelled()
        log.info("Waiting for attachment downloads for group %s...", group_name or group_id)
        # (syncing progress already sent during group verification above)

        def _trigger_attachments(gid: str, gname: str) -> dict:
            url = settings.signal_desktop_url.rstrip("/") + "/attachments/trigger"
            try:
                with httpx.Client(timeout=30) as client:
                    r = client.post(url, params={"group_id": gid, "group_name": gname})
                    r.raise_for_status()
                    return r.json()
            except Exception as e:
                log.warning("CDP attachment trigger failed (non-fatal): %s", e)
                return {"ok": False, "error": str(e)}

        def _get_group_att_status() -> dict:
            url = settings.signal_desktop_url.rstrip("/") + "/attachments/status"
            try:
                with httpx.Client(timeout=15) as client:
                    r = client.get(url, params={"group_id": group_id, "group_name": group_name})
                    r.raise_for_status()
                    return r.json()
            except Exception as e:
                log.warning("Could not get attachment status: %s", e)
                return {}

        # Best-effort CDP trigger — nudges Signal Desktop to prioritise this group.
        trigger_result = _trigger_attachments(group_id, group_name)
        log.info(
            "Attachment trigger (best-effort): ok=%s triggered=%s method=%s error=%s",
            trigger_result.get("ok"), trigger_result.get("triggered"),
            trigger_result.get("method"), trigger_result.get("error"),
        )

        # Run deep attachment diagnostics BEFORE trying to fetch.
        # Signal Desktop processes attachment metadata asynchronously —
        # wait for the downloads queue to include our group's attachments.
        def _run_diagnostics() -> dict:
            try:
                diag_url = settings.signal_desktop_url.rstrip("/") + "/attachments/diagnose"
                with httpx.Client(timeout=30) as client:
                    resp = client.get(diag_url, params={"group_id": group_id, "group_name": group_name})
                    if resp.status_code == 200:
                        return resp.json()
            except Exception as e:
                log.warning("Diagnostic call failed: %s", e)
            return {}

        diag = _run_diagnostics()
        log.info(
            "ATTACHMENT DIAGNOSTICS: has_table=%s table_total=%s "
            "hasAttachments_flag=%s legacy_json=%s downloads_queue=%s total_msgs=%s",
            diag.get("has_message_attachments_table"),
            (diag.get("message_attachments_table") or {}).get("total_attachments"),
            diag.get("has_attachments_flag_count"),
            (diag.get("legacy_json_column") or {}).get("messages_with_non_empty_attachments"),
            diag.get("attachment_downloads_queue"),
            diag.get("total_messages_in_group"),
        )
        legacy_json = diag.get("legacy_json_column") or {}
        if legacy_json.get("samples"):
            for s in legacy_json["samples"]:
                log.info("  JSON attachment sample: %s", json.dumps(s, ensure_ascii=False)[:600])
        if diag.get("messages_with_hasAttachments_samples"):
            for s in diag["messages_with_hasAttachments_samples"]:
                log.info("  hasAttachments sample: %s", json.dumps(s, ensure_ascii=False)[:500])

        # If the legacy json shows attachments but the table doesn't, wait
        # for Signal Desktop to process them (it moves json→table async)
        legacy_att_count = legacy_json.get("messages_with_non_empty_attachments", 0) or 0
        table_att_count = (diag.get("message_attachments_table") or {}).get("total_attachments", 0) or 0
        if legacy_att_count > 0 and table_att_count == 0:
            log.info(
                "Found %d messages with JSON attachments but 0 in table — "
                "waiting for Signal Desktop to process...",
                legacy_att_count,
            )
            for wait_round in range(6):  # up to 60s extra
                time.sleep(10)
                check_cancelled()
                diag = _run_diagnostics()
                new_table = (diag.get("message_attachments_table") or {}).get("total_attachments", 0) or 0
                new_flag = diag.get("has_attachments_flag_count", 0) or 0
                new_legacy = (diag.get("legacy_json_column") or {}).get("messages_with_non_empty_attachments", 0)
                log.info(
                    "  Wait round %d: table=%d hasFlag=%d legacy_json=%d queue=%s",
                    wait_round + 1, new_table, new_flag, new_legacy,
                    diag.get("attachment_downloads_queue"),
                )
                if new_table > 0 or new_flag > 0:
                    log.info("Attachments appeared in table/flag — proceeding")
                    break

        # Direct CDN fetch for messages that already have cdnKey+key in DB.
        _fetch_attachments_direct(settings, group_id=group_id, group_name=group_name)

        # Poll group-specific attachment status until ready or timeout.
        # Signal Desktop 7+ processes attachments asynchronously: first
        # hasAttachments flag is set, then entries appear in
        # attachment_downloads queue, and finally in message_attachments.
        # Our db_reader can extract metadata from attachment_downloads,
        # so we just need to wait for EITHER table or downloads to appear.
        MAX_ATT_WAIT = 180
        ATT_POLL = 5
        MIN_INITIAL_WAIT = 15
        att_start = time.time()
        time.sleep(MIN_INITIAL_WAIT)

        has_att_flag_count = 0
        group_dl_count = 0
        diag = _run_diagnostics()
        has_att_flag_count = diag.get("has_attachments_flag_count", 0) or 0
        group_dl_count = diag.get("group_attachment_downloads", 0) or 0
        table_count = (diag.get("message_attachments_table") or {}).get("total_attachments", 0) or 0
        log.info(
            "Attachment check: hasAttachments=%d table=%d group_downloads=%d global_queue=%s",
            has_att_flag_count, table_count, group_dl_count,
            diag.get("attachment_downloads_queue"),
        )
        if diag.get("group_download_samples"):
            for s in diag["group_download_samples"]:
                log.info("  Group download sample: %s", json.dumps(s, ensure_ascii=False)[:500])

        # Wait for downloads to appear for our group when hasAttachments > 0
        while has_att_flag_count > 0 and group_dl_count == 0 and table_count == 0:
            elapsed = time.time() - att_start
            if elapsed >= 90:
                log.info("Gave up waiting for group downloads after %.0fs", elapsed)
                break
            log.info(
                "Waiting for group attachment downloads (hasAtt=%d, groupDL=%d, table=%d, %.0fs)",
                has_att_flag_count, group_dl_count, table_count, elapsed,
            )
            time.sleep(ATT_POLL)
            check_cancelled()
            diag = _run_diagnostics()
            has_att_flag_count = diag.get("has_attachments_flag_count", 0) or 0
            group_dl_count = diag.get("group_attachment_downloads", 0) or 0
            table_count = (diag.get("message_attachments_table") or {}).get("total_attachments", 0) or 0

        # Once we have group downloads queued (or table populated), wait for
        # them to complete (i.e. path appears on disk via message_attachments).
        if group_dl_count > 0 or table_count > 0 or has_att_flag_count > 0:
            log.info(
                "Attachments found for group: table=%d downloads=%d hasFlag=%d — "
                "waiting for Signal Desktop to finish downloading...",
                table_count, group_dl_count, has_att_flag_count,
            )
            prev_dl = group_dl_count
            stall_rounds = 0
            while True:
                elapsed = time.time() - att_start
                if elapsed >= MAX_ATT_WAIT:
                    log.info(
                        "Attachment wait timeout (%.0fs): table=%d downloads=%d",
                        elapsed, table_count, group_dl_count,
                    )
                    break
                time.sleep(ATT_POLL)
                check_cancelled()
                s = _get_group_att_status()
                total_att = s.get("total_attachments", 0)
                ready_att = s.get("ready", 0)
                pending_att = s.get("pending", 0)
                diag = _run_diagnostics()
                group_dl_count = diag.get("group_attachment_downloads", 0) or 0
                table_count = (diag.get("message_attachments_table") or {}).get("total_attachments", 0) or 0
                log.info(
                    "  Download progress: table=%d(ready=%d) downloads_queue=%d (%.0fs)",
                    total_att, ready_att, group_dl_count, elapsed,
                )
                if total_att > 0 and pending_att == 0:
                    log.info("All %d attachment(s) ready", total_att)
                    break
                # If downloads queue is empty for our group but table still empty,
                # downloads might have completed — proceed to let db_reader check
                if group_dl_count == 0 and total_att == 0 and elapsed > 30:
                    log.info("Group download queue empty, proceeding with what we have")
                    break
                if group_dl_count == prev_dl:
                    stall_rounds += 1
                    if stall_rounds >= 8:
                        log.info("Download progress stalled for %d rounds, proceeding", stall_rounds)
                        break
                else:
                    stall_rounds = 0
                    prev_dl = group_dl_count
        else:
            log.info("No attachments found for group %s — skipping wait", group_name or group_id)

        # Second CDN fetch after wait — catches attachments that appeared during the wait period.
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

        # Tally image attachment readiness before enrichment
        msgs_with_atts = [m for m in msgs if m.get("attachments")]
        all_atts = [att for m in msgs for att in (m.get("attachments") or []) if isinstance(att, dict)]
        img_atts = [a for a in all_atts if (a.get("contentType") or "").startswith("image/")]
        img_on_disk = [a for a in img_atts if a.get("path")]
        img_cdn_ready = [a for a in img_atts if a.get("cdnKey") and not a.get("path")]
        img_pending = [a for a in img_atts if not a.get("path") and not a.get("cdnKey")]
        log.info(
            "Messages with attachments: %d/%d | Images: %d total"
            " (%d on disk, %d CDN-ready, %d pending/no-key)",
            len(msgs_with_atts), len(msgs),
            len(img_atts), len(img_on_disk), len(img_cdn_ready), len(img_pending),
        )
        for m in msgs_with_atts[:3]:
            for att in (m.get("attachments") or [])[:2]:
                log.info(
                    "  att sample: contentType=%s path=%r cdnKey=%r key_len=%d",
                    att.get("contentType"), att.get("path") or "",
                    (att.get("cdnKey") or "")[:20] or "(none)",
                    len(att.get("key") or ""),
                )
        _notify_progress(settings=settings, token=token, progress_key="found_messages", count=len(msgs))

        check_cancelled()

        # ?????????????????????????????????????????????????????????????????
        # Step 4b: Enrich messages that have attachments with OCR text
        # Each message dict gets an "enriched_body" and "image_payloads" key.
        #
        # TWO-PASS APPROACH:
        #   Pass 1: Enrich with whatever attachments are available now.
        #   Pass 2: For messages that had attachments but got 0 bytes,
        #           run another CDN fetch-all and re-try fetching just
        #           those attachments.
        # ?????????????????????????????????????????????????????????????????
        openai_client_early = OpenAI(
            api_key=settings.openai_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            max_retries=0,
        )
        enrich_start = time.time()
        msgs = _enrich_messages_with_attachments(
            settings=settings,
            openai_client=openai_client_early,
            messages=msgs,
        )

        # Pass 2: Re-try attachments that were missed on first pass
        missed = [
            m for m in msgs
            if m.get("attachments") and not m.get("image_payloads")
        ]
        if missed:
            log.info(
                "PASS 2: %d messages had attachments but got 0 bytes — "
                "running another CDN fetch-all and retrying...",
                len(missed),
            )
            time.sleep(10)
            check_cancelled()
            _fetch_attachments_direct(settings, group_id=group_id, group_name=group_name)
            time.sleep(5)

            re_enriched = _enrich_messages_with_attachments(
                settings=settings,
                openai_client=openai_client_early,
                messages=missed,
            )
            re_enriched_by_id = {}
            for m in re_enriched:
                mid = m.get("id") or m.get("message_id") or str(m.get("ts", 0))
                re_enriched_by_id[mid] = m

            recovered = 0
            for i, m in enumerate(msgs):
                mid = m.get("id") or m.get("message_id") or str(m.get("ts", 0))
                if mid in re_enriched_by_id:
                    replacement = re_enriched_by_id[mid]
                    if replacement.get("image_payloads"):
                        msgs[i] = replacement
                        recovered += 1
            log.info(
                "PASS 2 complete: recovered %d/%d missed attachments",
                recovered, len(missed),
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
        n_chunks = len(chunks)
        max_workers = min(n_chunks, 10)

        step5_start = time.time()
        if n_chunks <= 1:
            for i, (ch, ch_images) in enumerate(chunks):
                check_cancelled()
                t0 = time.time()
                result = _extract_structured_cases(
                    openai_client=openai_client_early,
                    model=settings.model_blocks,
                    fallback_models=settings.model_blocks_fallback,
                    chunk_text=ch,
                    images=ch_images or None,
                )
                log.info("Chunk %d/%d extracted %d cases in %.1fs", i+1, n_chunks, len(result), time.time()-t0)
                all_structured.extend(result)
        else:
            completed = 0
            futures = {}
            chunk_start_times = {}
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                for i, (ch, ch_images) in enumerate(chunks):
                    check_cancelled()
                    chunk_start_times[i] = time.time()
                    fut = executor.submit(
                        _extract_structured_cases,
                        openai_client=openai_client_early,
                        model=settings.model_blocks,
                        fallback_models=settings.model_blocks_fallback,
                        chunk_text=ch,
                        images=ch_images or None,
                    )
                    futures[fut] = i

                for fut in as_completed(futures):
                    check_cancelled()
                    completed += 1
                    chunk_idx = futures[fut]
                    elapsed_chunk = time.time() - chunk_start_times[chunk_idx]
                    _notify_progress(settings=settings, token=token, progress_key="processing_chunk", current=completed, total=n_chunks)
                    try:
                        result = fut.result()
                        log.info("Chunk %d/%d extracted %d cases in %.1fs", chunk_idx+1, n_chunks, len(result), elapsed_chunk)
                        all_structured.extend(result)
                    except Exception:
                        log.exception("Chunk %d failed after %.1fs, skipping", chunk_idx, elapsed_chunk)
                        continue
        log.info("All %d chunks processed in %.1fs — %d raw cases", n_chunks, time.time()-step5_start, len(all_structured))

        dedup_start = time.time()
        deduped = _dedup_cases_llm(
            openai_client=openai_client_early,
            model=settings.model_blocks,
            fallback_models=settings.model_blocks_fallback,
            cases=all_structured,
        )
        log.info("Dedup completed in %.1fs: %d → %d cases", time.time()-dedup_start, len(all_structured), len(deduped))

        # ─────────────────────────────────────────────────────────────
        # Step 6: Post structured cases to signal-bot (batch embed on bot side)
        # ─────────────────────────────────────────────────────────────
        cases_inserted = 0
        if deduped:
            _notify_progress(settings=settings, token=token, progress_key="saving_cases", count=len(deduped))
            cases_inserted = _post_structured_cases_to_bot(
                settings=settings, token=token, group_id=group_id,
                structured_cases=deduped, messages=msgs,
            )
        else:
            log.info("No solved cases found in messages")

        # Report attachment stats
        total_payloads = sum(len(m.get("image_payloads") or []) for m in msgs)
        total_with_att = sum(1 for m in msgs if m.get("attachments"))
        note = ""
        if total_with_att:
            note = f"Images: {total_payloads} fetched from {total_with_att} messages with attachments."

        _notify_link_result(
            settings=settings,
            token=token,
            success=True,
            message_count=len(msgs),
            cases_found=len(deduped),
            cases_inserted=cases_inserted,
            note=note,
        )
        
        # SECURITY: Reset Signal Desktop session after successful ingest.
        # User's account is unlinked; requires new QR scan next time.
        log.info("Resetting Signal Desktop session for security (unlinking user account)...")
        try:
            _reset_desktop(settings)
            log.info("Signal Desktop session reset successfully")
        except Exception as e:
            log.warning("Failed to reset Signal Desktop session: %s", e)

    except JobCancelled:
        log.info("Job %d was cancelled", job_id)
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
