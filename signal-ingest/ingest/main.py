from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Any, Dict, List, Optional

import httpx
import qrcode
from openai import OpenAI

from ingest.config import load_settings
from ingest.db import claim_next_job, complete_job, create_db, fail_job

HISTORY_LINK = "HISTORY_LINK"
HISTORY_SYNC = "HISTORY_SYNC"

P_BLOCKS_SYSTEM = """From a long history chunk, extract solved support cases.
Return ONLY JSON with key:
- cases: array of objects, each with:
  - case_block: string (raw messages subset)
Do NOT return open/unresolved cases.

Rules:
- Each case_block must contain both problem and solution.
- Ignore greetings and unrelated chatter.
- Keep case_block as exact excerpts from the chunk.
"""

log = logging.getLogger(__name__)


class RetrySoon(Exception):
    pass


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _signal_cli_available(bin_name: str) -> bool:
    return shutil.which(bin_name) is not None


def _extract_tsdevice_uri(output: str) -> str:
    m = re.search(r"(tsdevice:[^\s]+)", output)
    if not m:
        raise RuntimeError("Could not find tsdevice: URI in signal-cli output")
    return m.group(1)


def _ensure_qr_png(*, settings, token: str) -> Path:
    out_dir = Path(settings.history_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{token}.png"
    if out_path.exists():
        return out_path

    device_name = f"SupportBotIngest-{token[:8]}"
    if not _signal_cli_available(settings.signal_cli):
        raise RuntimeError(f"signal-cli binary not found: {settings.signal_cli}")

    cmd = [
        settings.signal_cli,
        "--config",
        settings.signal_ingest_storage,
        "link",
        "-n",
        device_name,
    ]

    log.info("Generating link QR via signal-cli (device_name=%s)", device_name)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.stdout:
        log.info("signal-cli stdout: %s", proc.stdout.strip())
    if proc.stderr:
        log.info("signal-cli stderr: %s", proc.stderr.strip())
    if proc.returncode != 0:
        raise RuntimeError(f"signal-cli link failed (exit {proc.returncode})")

    uri = _extract_tsdevice_uri(proc.stdout + "\n" + proc.stderr)
    img = qrcode.make(uri)
    img.save(out_path)
    log.info("Wrote QR PNG: %s", out_path)
    return out_path


def _parse_receive_json(obj: dict) -> Optional[dict]:
    env = obj.get("envelope") if isinstance(obj, dict) else None
    if not isinstance(env, dict):
        return None
    ts = env.get("timestamp")
    dm = env.get("dataMessage")
    if ts is None or not isinstance(dm, dict):
        return None
    group_info = dm.get("groupInfo")
    group_id = group_info.get("groupId") if isinstance(group_info, dict) else None
    if not group_id:
        return None
    sender = (
        env.get("sourceUuid")
        or env.get("sourceNumber")
        or env.get("source")
        or env.get("sourceAddress")
        or ""
    )
    text = dm.get("message") or dm.get("body") or ""
    try:
        ts_i = int(ts)
    except Exception:
        return None
    return {"group_id": str(group_id), "sender": str(sender), "ts": ts_i, "text": str(text or "")}


def _collect_history_messages(*, settings, admin_id: str, target_group_id: str) -> List[dict]:
    if not _signal_cli_available(settings.signal_cli):
        raise RuntimeError(f"signal-cli binary not found: {settings.signal_cli}")

    cmd = [
        settings.signal_cli,
        "--config",
        settings.signal_ingest_storage,
        "-u",
        admin_id,
        "receive",
        "--output",
        "json",
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    assert proc.stdout is not None
    assert proc.stderr is not None

    q_lines: Queue[str] = Queue()

    def _read_stdout() -> None:
        for line in proc.stdout:
            if line:
                q_lines.put(line)

    def _read_stderr() -> None:
        for line in proc.stderr:
            if line and line.strip():
                log.info("signal-cli stderr: %s", line.strip())

    Thread(target=_read_stdout, daemon=True).start()
    Thread(target=_read_stderr, daemon=True).start()

    messages: List[dict] = []
    buf = ""
    seen_any = False
    last_line_time = time.time()
    deadline = time.time() + settings.history_max_seconds

    while time.time() < deadline:
        try:
            line = q_lines.get(timeout=1.0)
        except Empty:
            if seen_any and (time.time() - last_line_time) > settings.history_idle_seconds:
                break
            continue

        seen_any = True
        last_line_time = time.time()
        buf += line
        try:
            obj = json.loads(buf)
        except json.JSONDecodeError:
            continue

        buf = ""
        parsed = _parse_receive_json(obj)
        if parsed is None or parsed["group_id"] != target_group_id:
            continue
        messages.append(parsed)

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

    return messages


def _chunk_messages(*, messages: List[dict], max_chars: int, overlap_messages: int) -> List[str]:
    formatted = [f'{m["sender"]} ts={m["ts"]}\n{m["text"]}\n' for m in messages if m.get("text")]
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


def _extract_case_blocks(*, openai_client: OpenAI, model: str, chunk_text: str) -> List[str]:
    resp = openai_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": P_BLOCKS_SYSTEM},
            {"role": "user", "content": f"HISTORY_CHUNK:\n{chunk_text}"},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    raw = resp.choices[0].message.content or "{}"
    data = json.loads(raw)
    out: List[str] = []
    cases = data.get("cases", [])
    if isinstance(cases, list):
        for c in cases:
            if isinstance(c, dict) and isinstance(c.get("case_block"), str) and c["case_block"].strip():
                out.append(c["case_block"].strip())
    return out


def _post_cases_to_bot(*, settings, token: str, group_id: str, case_blocks: List[str]) -> None:
    payload = {"token": token, "group_id": group_id, "cases": [{"case_block": b} for b in case_blocks]}
    url = settings.signal_bot_url.rstrip("/") + "/history/cases"
    with httpx.Client(timeout=60) as client:
        r = client.post(url, json=payload)
        r.raise_for_status()
        log.info("Posted %s mined cases to signal-bot", len(case_blocks))


def _handle_history_link(*, settings, payload: Dict[str, Any]) -> None:
    token = str(payload["token"])
    admin_id = str(payload["admin_id"])
    group_id = str(payload["group_id"])

    _ensure_qr_png(settings=settings, token=token)

    msgs = _collect_history_messages(settings=settings, admin_id=admin_id, target_group_id=group_id)
    if not msgs:
        raise RetrySoon("No history messages received yet (device may not be linked yet)")

    chunks = _chunk_messages(
        messages=msgs,
        max_chars=settings.chunk_max_chars,
        overlap_messages=settings.chunk_overlap_messages,
    )

    # Use Google's OpenAI-compatible endpoint for Gemini models
    openai_client = OpenAI(
        api_key=settings.openai_api_key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    case_blocks: List[str] = []
    for ch in chunks:
        case_blocks.extend(_extract_case_blocks(openai_client=openai_client, model=settings.model_blocks, chunk_text=ch))

    deduped = list(dict.fromkeys([b for b in case_blocks if b.strip()]))
    if deduped:
        _post_cases_to_bot(settings=settings, token=token, group_id=group_id, case_blocks=deduped)
    else:
        log.info("No solved cases found in synced history (group_id=%s)", group_id)


def main() -> None:
    _configure_logging()
    settings = load_settings()
    db = create_db(settings)

    log.info("signal-ingest started (poll=%.2fs)", settings.worker_poll_seconds)

    while True:
        job = claim_next_job(db, allowed_types=[HISTORY_LINK, HISTORY_SYNC])
        if job is None:
            time.sleep(settings.worker_poll_seconds)
            continue

        try:
            if job.type == HISTORY_LINK:
                _handle_history_link(settings=settings, payload=job.payload)
            elif job.type == HISTORY_SYNC:
                raise NotImplementedError("HISTORY_SYNC is not used (history runs as part of HISTORY_LINK)")
            else:
                raise RuntimeError(f"Unknown job type: {job.type}")

            complete_job(db, job_id=job.job_id)
        except RetrySoon:
            log.warning("History not ready yet; will retry soon (job_id=%s)", job.job_id)
            fail_job(db, job_id=job.job_id, attempts=job.attempts, max_attempts=1000)
        except Exception:
            log.exception("Job failed: id=%s type=%s", job.job_id, job.type)
            fail_job(db, job_id=job.job_id, attempts=job.attempts)


if __name__ == "__main__":
    main()

