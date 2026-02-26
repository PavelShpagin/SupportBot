#!/usr/bin/env python3
"""
Local ingestion runner.

Runs the full case-extraction pipeline on the sample_chat.json fixture
(augmented with a synthetic image message), then optionally POSTs to prod.

Usage:
    python3 run_local_ingest.py              # POST to prod, get working links
    python3 run_local_ingest.py --local      # In-memory only, links are local IDs (404 on prod)
    python3 run_local_ingest.py --local --post-to-prod  # Local processing + POST to prod for working links
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import struct
import sys
import uuid
import zlib
import subprocess
import tempfile
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent  # when script lives in legacy/, parent = repo root

# ── sys.path ──────────────────────────────────────────────────────────────────
for d in [str(REPO_ROOT / "signal-ingest"), str(REPO_ROOT / "signal-bot")]:
    if d not in sys.path:
        sys.path.insert(0, d)

for lib in ("chromadb", "google", "google.generativeai", "mysql", "mysql.connector",
            "mysql.connector.errors"):
    if lib not in sys.modules:
        sys.modules[lib] = MagicMock()

# ── env ───────────────────────────────────────────────────────────────────────
env_path = REPO_ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"'))

API_KEY   = os.environ.get("GOOGLE_API_KEY", "")
MODEL     = os.environ.get("MODEL_BLOCKS", "gemini-3.1-pro-preview")
MODEL_IMG = os.environ.get("MODEL_IMG", "gemini-3.1-pro-preview")
PROD_URL  = "https://supportbot.info"

if not API_KEY:
    sys.exit("GOOGLE_API_KEY not set")

# ── generate a real PNG (320×120 error-screen mockup) ─────────────────────────
def _make_png(width: int, height: int, pixels_rgb) -> bytes:
    def chunk(tag, data):
        c = struct.pack('>I', len(data)) + tag + data
        return c + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
    raw = b''
    for row in pixels_rgb:
        raw += b'\x00'
        for r, g, b in row:
            raw += bytes([r, g, b])
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(raw)
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', ihdr)
            + chunk(b'IDAT', idat)
            + chunk(b'IEND', b''))

W, H = 320, 120
pixels = []
for y in range(H):
    row = []
    for x in range(W):
        if y < 30:
            row.append((30, 30, 80))    # title bar
        elif y < 35:
            row.append((200, 200, 200)) # separator
        elif 40 < y < 70 and 10 < x < 310:
            row.append((220, 50, 50))   # red error box
        elif 75 < y < 95 and 10 < x < 200:
            row.append((240, 240, 240)) # text area
        else:
            row.append((245, 245, 245))
    pixels.append(row)

_PNG_BYTES = _make_png(W, H, pixels)
_PNG_B64   = base64.b64encode(_PNG_BYTES).decode()
print(f"Generated PNG: {len(_PNG_BYTES)} bytes")

# ── load fixture ──────────────────────────────────────────────────────────────
fixture_path = REPO_ROOT / "tests" / "fixtures" / "sample_chat.json"
data     = json.loads(fixture_path.read_text(encoding="utf-8"))
messages = data["messages"]
# Use GROUP_ID env or "group-x" for test runs (avoids long fixture group_id)
group_id = os.environ.get("GROUP_ID", "group-x")

# ── inject synthetic image conversation ───────────────────────────────────────
_base_ts    = messages[-1]["ts"] + 10_000
_img_sender = messages[0]["sender"]
_adm_sender = messages[1]["sender"]

_img_id = "local-img-" + uuid.uuid4().hex[:8]
_adm_id = "local-adm-" + uuid.uuid4().hex[:8]
_cnf_id = "local-cnf-" + uuid.uuid4().hex[:8]

_img_msg = {
    "ts": _base_ts,
    "sender": _img_sender,
    "sender_name": "Alpha User",
    "body": "Screensharing issues – black screen on startup [image]",
    "id": _img_id,
    "reactions": 0,
    "attachments": [{"path": "attachments.noindex/local/error_screen.png",
                     "contentType": "image/png", "fileName": "error_screen.png"}],
    "_image_payload": {"filename": "error_screen.png",
                       "content_type": "image/png", "data_b64": _PNG_B64},
}
_adm_msg = {
    "ts": _base_ts + 3_000,
    "sender": _adm_sender,
    "sender_name": "Beta Admin",
    "body": "Restart the display service: sudo systemctl restart display-manager. This clears the init error.",
    "id": _adm_id,
    "reactions": 1,
    "reaction_emoji": "👍",
}
_cnf_msg = {
    "ts": _base_ts + 6_000,
    "sender": _img_sender,
    "sender_name": "Alpha User",
    "body": "Worked! Thanks.",
    "id": _cnf_id,
    "reactions": 0,
}

# Optionally pad to ~N messages (e.g. TARGET_MESSAGES=180 for larger ingest test)
_target = int(os.environ.get("TARGET_MESSAGES", "0"))
if _target == 0 and "--local" in sys.argv:
    _target = 180  # default for local run
_pad = messages + [_img_msg, _adm_msg, _cnf_msg]
if _target > len(_pad):
    while len(_pad) < _target:
        for m in messages[:20]:  # cycle through first 20 to add variety
            if len(_pad) >= _target:
                break
            clone = dict(m)
            clone["id"] = "pad-" + uuid.uuid4().hex[:12]
            clone["ts"] = clone["ts"] + len(_pad) * 10000
            _pad.append(clone)
messages_augmented = _pad

# ── LLM clients ───────────────────────────────────────────────────────────────
os.environ.setdefault("SIGNAL_BOT_E164", "+10000000000")
os.environ.setdefault("DB_BACKEND", "mysql")
os.environ["SIGNAL_LISTENER_ENABLED"] = "false"
os.environ["USE_SIGNAL_DESKTOP"] = "false"

from openai import OpenAI
oc = OpenAI(
    api_key=API_KEY,
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    timeout=120.0,  # Prevent indefinite hang on chunk 2 (Gemini API)
)

from app.llm.client import LLMClient
_s = MagicMock()
_s.openai_api_key  = API_KEY
_s.model_case      = MODEL
_s.embedding_model = os.getenv("EMBEDDING_MODEL", "gemini-embedding-001")
llm = LLMClient(_s)

from ingest.main import _chunk_messages, _ocr_attachment, _extract_structured_cases, _dedup_cases_llm

# ── OCR the image ─────────────────────────────────────────────────────────────
print("\n>> OCR-ing image message...")
ocr_json = _ocr_attachment(
    openai_client=oc, model=MODEL_IMG,
    image_bytes=_PNG_BYTES, content_type="image/png",
    context_text=_img_msg["body"],
)
print(f"  OCR: {ocr_json[:120]}")

# Enrich body with OCR result — same format as ingestion.py (human-readable, not raw JSON)
if ocr_json:
    try:
        ocr_data = json.loads(ocr_json)
        extracted_text = ocr_data.get("extracted_text") or ""
        observations   = ocr_data.get("observations") or []
        parts = []
        if extracted_text:
            parts.append(f"Текст на зображенні: {extracted_text}")
        if observations:
            parts.append(f"Елементи на зображенні: {', '.join(observations)}")
        if parts:
            _img_msg["body"] = _img_msg["body"] + "\n\n[Зображення: " + " | ".join(parts) + "]"
    except Exception:
        pass  # OCR parse failed — keep body as-is

# ── Phase 1: chunk + extract structured cases (8x fewer API calls) ────────────
_chunk_max = int(os.environ.get("HISTORY_CHUNK_MAX_CHARS", "45000"))
_chunk_overlap = int(os.environ.get("HISTORY_CHUNK_OVERLAP_MESSAGES", "1"))
print(f"\n>> Chunking {len(messages_augmented)} messages (max_chars={_chunk_max})...")
chunks = _chunk_messages(messages=messages_augmented, max_chars=_chunk_max,
                         overlap_messages=_chunk_overlap, bot_e164="")
print(f"  → {len(chunks)} chunk(s)")

print("\n>> Extracting structured cases (LLM)...")
all_structured: List[dict] = []
worker_script = ROOT / "extract_chunk_worker.py"
for i in range(len(chunks)):
    print(f"  chunk {i+1}/{len(chunks)}...", end=" ", flush=True)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tf:
        json.dump({"chunk_text": chunks[i], "api_key": API_KEY, "model": MODEL, "structured": True}, tf, ensure_ascii=False)
        tmp_path = tf.name
    try:
        result = subprocess.run(
            [sys.executable, str(worker_script), tmp_path],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=300,
            env={**os.environ, "GOOGLE_API_KEY": API_KEY},
        )
        if result.returncode != 0:
            raise RuntimeError(f"Worker failed: {result.stderr or result.stdout}")
        cases = json.loads(result.stdout)
        all_structured.extend(cases)
        print(f"{len(cases)} case(s)")
    finally:
        os.unlink(tmp_path)

# LLM dedup
print("  dedup (LLM)...", end=" ", flush=True)
deduped = _dedup_cases_llm(openai_client=oc, model=MODEL, cases=all_structured)
print(f"{len(deduped)} case(s)")

# If the image case wasn't extracted, inject it manually
img_found = any(_img_id in c.get("case_block", "") or "display-manager" in (c.get("solution_summary") or "") for c in deduped)
if not img_found:
    print("  ⚠ Image case not extracted — injecting manually")
    _img_hash = hashlib.sha256(_img_sender.encode()).hexdigest()[:16]
    _adm_hash = hashlib.sha256(_adm_sender.encode()).hexdigest()[:16]
    manual_case = {
        "keep": True,
        "status": "solved",
        "problem_title": "Чорний екран під час запуску демонстрації екрана",
        "problem_summary": "Screensharing issues – black screen on startup.",
        "solution_summary": "Restart the display service: sudo systemctl restart display-manager.",
        "tags": ["display-manager", "screenshare", "black-screen"],
        "evidence_ids": [_img_id, _adm_id, _cnf_id],
        "case_block": (
            f"{_img_hash} ts={_base_ts} msg_id={_img_id} reactions=0\n"
            f"{_img_msg['body']}\n\n"
            f"{_adm_hash} ts={_base_ts+3000} msg_id={_adm_id} reactions=1 reaction_emoji=👍\n"
            f"{_adm_msg['body']}\n\n"
            f"{_img_hash} ts={_base_ts+6000} msg_id={_cnf_id} reactions=0\n"
            f"{_cnf_msg['body']}"
        ),
    }
    deduped.append(manual_case)

print(f"  → {len(deduped)} structured cases total")

# ── Local-only mode: batch embed, DB dedup (no make_case per block) ──────────
if "--local" in sys.argv:
    import sqlite3
    from contextlib import contextmanager

    class _LocalCursor:
        def __init__(self, cur): self._cur = cur
        def execute(self, sql, params=()):
            sql = sql.replace("%s", "?")
            if "JOIN case_evidence ce ON ce.case_id = c.case_id" in sql:
                sql = """UPDATE cases SET closed_emoji = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE case_id IN (SELECT ce.case_id FROM case_evidence ce WHERE ce.message_id = ?)
                    AND status IN ('solved','open') AND closed_emoji IS NULL"""
            self._cur.execute(sql, params)
            self.rowcount = self._cur.rowcount
        def fetchone(self): return self._cur.fetchone()
        def fetchall(self): return self._cur.fetchall()

    class _LocalConn:
        def __init__(self, conn): self._conn = conn
        def cursor(self): return _LocalCursor(self._conn.cursor())
        def commit(self): self._conn.commit()
        def rollback(self): self._conn.rollback()

    class FakeDB:
        def __init__(self):
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.create_function("NOW", 0, lambda: "2000-01-01 00:00:00")
            c = self._conn.cursor()
            c.executescript("""
                CREATE TABLE cases (case_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, status TEXT,
                problem_title TEXT, problem_summary TEXT, solution_summary TEXT, tags_json TEXT,
                evidence_image_paths_json TEXT, in_rag INTEGER DEFAULT 0, closed_emoji TEXT,
                embedding_json TEXT, created_at TIMESTAMP, updated_at TIMESTAMP);
                CREATE TABLE case_evidence (case_id TEXT, message_id TEXT, PRIMARY KEY(case_id,message_id));
                CREATE TABLE raw_messages (message_id TEXT PRIMARY KEY, group_id TEXT, ts INTEGER, sender_hash TEXT, sender_name TEXT, content_text TEXT, image_paths_json TEXT, reply_to_id TEXT);
            """)
            self._conn.commit()

        @contextmanager
        def connection(self):
            yield _LocalConn(self._conn)

        def all_cases(self):
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM cases ORDER BY created_at")
            return [dict(r) for r in cur.fetchall()]

    from app.db.queries_mysql import upsert_case, find_similar_case, merge_case, store_case_embedding

    db = FakeDB()
    group_id = os.environ.get("GROUP_ID", "group-x")
    inserted = updated = 0
    # Batch embed (1 API call for all cases)
    embed_texts = [f"{c.get('problem_title','')}\n{c.get('problem_summary','')}" for c in deduped]
    embeddings = llm.embed_batch(texts=embed_texts)
    for i, case in enumerate(deduped):
        evidence_ids = list(case.get("evidence_ids") or [])
        if not evidence_ids:
            for line in (case.get("case_block") or "").split("\n"):
                m = re.search(r"msg_id=(\S+)", line)
                if m:
                    evidence_ids.append(m.group(1))
        similar_id = find_similar_case(db, group_id=group_id, embedding=embeddings[i])
        if similar_id:
            merge_case(db, target_case_id=similar_id, status=case.get("status", "solved"),
                problem_summary=case.get("problem_summary", ""), solution_summary=case.get("solution_summary", "") or "",
                tags=case.get("tags") or [], evidence_ids=evidence_ids, evidence_image_paths=[])
            store_case_embedding(db, similar_id, embeddings[i])
            updated += 1
        else:
            cid = uuid.uuid4().hex
            final_id, created = upsert_case(db, case_id=cid, group_id=group_id, status=case.get("status", "solved"),
                problem_title=case.get("problem_title", ""), problem_summary=case.get("problem_summary", ""),
                solution_summary=case.get("solution_summary", "") or "", tags=case.get("tags") or [],
                evidence_ids=evidence_ids, evidence_image_paths=[])
            store_case_embedding(db, final_id, embeddings[i])
            inserted += 1 if created else 0
            updated += 0 if created else 1

    final_cases = db.all_cases()
    print(f"\n{'='*65}")
    print(f"LOCAL PIPELINE RESULT: {len(messages_augmented)} messages -> {len(deduped)} structured cases")
    print(f"  Inserted: {inserted}  Merged: {updated}")
    print(f"  FINAL CASES: {len(final_cases)}")
    print(f"{'='*65}")
    if "--post-to-prod" not in sys.argv:
        for i, c in enumerate(final_cases, 1):
            emoji = f" [{c.get('closed_emoji')}]" if c.get("closed_emoji") else ""
            print(f"  [{i}] [{c.get('status','')}{emoji}] {c.get('problem_title','')[:55]}")
            print(f"       (local ID, not on prod — add --post-to-prod for working links)")
        print(f"{'='*65}\n")
        sys.exit(0)
    print("  Posting to prod for working links...")
    print(f"{'='*65}\n")

# ── Build messages payload (what gets posted to /history/cases) ───────────────
def _sender_hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]

messages_payload = []
for msg in messages_augmented:
    text = msg.get("body") or ""
    img_payloads = []
    if msg.get("_image_payload"):
        p = msg["_image_payload"]
        img_payloads.append({
            "filename": p["filename"],
            "content_type": p["content_type"],
            "data_b64": p["data_b64"],
        })
    if not text and not img_payloads:
        continue
    messages_payload.append({
        "message_id": msg["id"],
        "sender_hash": _sender_hash(msg["sender"]),
        "sender_name": msg.get("sender_name"),
        "ts": msg["ts"],
        "content_text": text,
        "image_payloads": img_payloads,
    })

# ── POST to prod ──────────────────────────────────────────────────────────────
import urllib.request

print(f"\n>> Creating debug token on prod...")
token_req = json.dumps({"admin_id": "local-test", "group_id": group_id}).encode()
req = urllib.request.Request(
    f"{PROD_URL}/history/token",
    data=token_req, headers={"Content-Type": "application/json"}, method="POST",
)
with urllib.request.urlopen(req) as r:
    token = json.load(r)["token"]
print(f"  token: {token[:16]}...")

print(f">> Posting {len(deduped)} structured cases + {len(messages_payload)} messages...")
history_req = json.dumps({
    "token": token,
    "group_id": group_id,
    "cases_structured": [
        {
            "case_block": c.get("case_block", ""),
            "problem_title": c.get("problem_title", ""),
            "problem_summary": c.get("problem_summary", ""),
            "solution_summary": c.get("solution_summary", ""),
            "status": c.get("status", "solved"),
            "tags": c.get("tags") or [],
            "evidence_ids": c.get("evidence_ids") or [],
        }
        for c in deduped
    ],
    "messages": messages_payload,
}).encode()
req2 = urllib.request.Request(
    f"{PROD_URL}/history/cases",
    data=history_req, headers={"Content-Type": "application/json"}, method="POST",
)
with urllib.request.urlopen(req2) as r:
    result = json.load(r)

print(f"  inserted: {result.get('cases_inserted')} / case_ids: {len(result.get('case_ids', []))}")

# ── Report: fetch REAL case links from API (not from response order) ───────────
print(f"\n{'='*65}")
print(f"PIPELINE RESULT: {len(messages_augmented)} messages → {len(chunks)} chunk(s)")
print(f"  Structured cases     : {len(deduped)}")
print(f"  Posted to prod       : {result.get('cases_inserted')} inserted")
print(f"{'='*65}")

# Fetch actual cases from API for this group — do not hallucinate or reuse IDs
try:
    cases_req = urllib.request.Request(
        f"{PROD_URL}/api/group-cases?group_id={group_id}&include_archived=true",
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(cases_req, timeout=15) as r:
        cases_data = json.load(r)
    cases_list = cases_data.get("cases", [])
    for i, c in enumerate(cases_list, 1):
        cid = c.get("case_id", "")
        title = (c.get("problem_title") or "")[:55]
        print(f"  [{i}] {PROD_URL}/case/{cid}  ({title})")
    # Mark image case if we have one with evidence containing the synthetic image
    img_case = next((c for c in cases_list if "display-manager" in (c.get("solution_summary") or "")
                    or "Screensharing" in (c.get("problem_title") or "")), None)
    if img_case:
        print(f"\n  [IMAGE] {PROD_URL}/case/{img_case.get('case_id', '')}")
except Exception as e:
    print(f"  (Could not fetch case links from API: {e})")
print(f"{'='*65}")
