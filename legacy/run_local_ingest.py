#!/usr/bin/env python3
"""
Local ingestion runner.

Runs the full case-extraction pipeline on the sample_chat.json fixture
(augmented with a synthetic image message), then POSTs the raw LLM-extracted
case blocks + messages to the prod /history/cases endpoint.

Usage:
    python3 run_local_ingest.py
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
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

ROOT = Path(__file__).parent

# ── sys.path ──────────────────────────────────────────────────────────────────
for d in [str(ROOT / "signal-ingest"), str(ROOT / "signal-bot")]:
    if d not in sys.path:
        sys.path.insert(0, d)

for lib in ("chromadb", "google", "google.generativeai", "mysql", "mysql.connector",
            "mysql.connector.errors"):
    if lib not in sys.modules:
        sys.modules[lib] = MagicMock()

# ── env ───────────────────────────────────────────────────────────────────────
env_path = ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"'))

API_KEY   = os.environ.get("GOOGLE_API_KEY", "")
MODEL     = os.environ.get("MODEL_BLOCKS", "gemini-2.5-flash-lite")
MODEL_IMG = os.environ.get("MODEL_IMG", "gemini-2.0-flash")
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
fixture_path = ROOT / "tests" / "fixtures" / "sample_chat.json"
data     = json.loads(fixture_path.read_text(encoding="utf-8"))
messages = data["messages"]
group_id = data["group_id"]

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

messages_augmented = messages + [_img_msg, _adm_msg, _cnf_msg]

# ── LLM clients ───────────────────────────────────────────────────────────────
os.environ.setdefault("SIGNAL_BOT_E164", "+10000000000")
os.environ.setdefault("DB_BACKEND", "mysql")
os.environ["SIGNAL_LISTENER_ENABLED"] = "false"
os.environ["USE_SIGNAL_DESKTOP"] = "false"

from openai import OpenAI
oc = OpenAI(api_key=API_KEY,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/")

from app.llm.client import LLMClient
_s = MagicMock()
_s.openai_api_key  = API_KEY
_s.model_case      = MODEL
_s.embedding_model = os.getenv("EMBEDDING_MODEL", "gemini-embedding-001")
llm = LLMClient(_s)

from ingest.main import _chunk_messages, _extract_case_blocks, _ocr_attachment

# ── OCR the image ─────────────────────────────────────────────────────────────
print("\n▶ OCR-ing image message...")
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

# ── Phase 1: chunk + extract raw case blocks ──────────────────────────────────
print(f"\n▶ Chunking {len(messages_augmented)} messages...")
chunks = _chunk_messages(messages=messages_augmented, max_chars=12000,
                         overlap_messages=3, bot_e164="")
print(f"  → {len(chunks)} chunk(s)")

print("\n▶ Extracting case blocks (LLM)...")
raw_blocks: List[str] = []
seen_keys: set = set()
for i, chunk in enumerate(chunks):
    print(f"  chunk {i+1}/{len(chunks)}...", end=" ", flush=True)
    blocks = _extract_case_blocks(openai_client=oc, model=MODEL, chunk_text=chunk)
    new = 0
    for b in blocks:
        key = b[:120]
        if key not in seen_keys:
            seen_keys.add(key)
            raw_blocks.append(b)
            new += 1
    print(f"{new} block(s)")

# If the image case wasn't extracted (LLM missed it), inject it manually
img_block_found = any(_img_id in b or "display-manager" in b for b in raw_blocks)
if not img_block_found:
    print("  ⚠ Image case not extracted by LLM — injecting manually")
    _img_hash = hashlib.sha256(_img_sender.encode()).hexdigest()[:16]
    _adm_hash = hashlib.sha256(_adm_sender.encode()).hexdigest()[:16]
    manual_block = (
        f"{_img_hash} ts={_base_ts} msg_id={_img_id} reactions=0\n"
        f"{_img_msg['body']}\n\n"
        f"{_adm_hash} ts={_base_ts+3000} msg_id={_adm_id} reactions=1 reaction_emoji=👍\n"
        f"{_adm_msg['body']}\n\n"
        f"{_img_hash} ts={_base_ts+6000} msg_id={_cnf_id} reactions=0\n"
        f"{_cnf_msg['body']}"
    )
    raw_blocks.append(manual_block)

print(f"  → {len(raw_blocks)} raw blocks total")

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

print(f"\n▶ Creating debug token on prod...")
token_req = json.dumps({"admin_id": "local-test", "group_id": group_id}).encode()
req = urllib.request.Request(
    f"{PROD_URL}/history/token",
    data=token_req, headers={"Content-Type": "application/json"}, method="POST",
)
with urllib.request.urlopen(req) as r:
    token = json.load(r)["token"]
print(f"  token: {token[:16]}...")

print(f"▶ Posting {len(raw_blocks)} case blocks + {len(messages_payload)} messages...")
history_req = json.dumps({
    "token": token,
    "group_id": group_id,
    "cases": [{"case_block": b} for b in raw_blocks],
    "messages": messages_payload,
}).encode()
req2 = urllib.request.Request(
    f"{PROD_URL}/history/cases",
    data=history_req, headers={"Content-Type": "application/json"}, method="POST",
)
with urllib.request.urlopen(req2) as r:
    result = json.load(r)

case_ids: List[str] = result.get("case_ids", [])
print(f"  inserted: {result.get('cases_inserted')} / case_ids: {len(case_ids)}")

# ── Report ────────────────────────────────────────────────────────────────────
print(f"\n{'='*65}")
print(f"PIPELINE RESULT: {len(messages_augmented)} messages → {len(chunks)} chunk(s)")
print(f"  Raw blocks extracted : {len(raw_blocks)}")
print(f"  Posted to prod       : {result.get('cases_inserted')} inserted")
print(f"{'='*65}")
for i, cid in enumerate(case_ids, 1):
    print(f"  [{i}] {PROD_URL}/case/{cid}")
print(f"{'='*65}")

# Identify the image case (last block = image case if injected, or whichever has _img_id)
img_case_id = None
for i, (b, cid) in enumerate(zip(raw_blocks, case_ids)):
    if _img_id in b or "display-manager" in b or "error_screen" in b:
        img_case_id = cid
        break
if not img_case_id and case_ids:
    img_case_id = case_ids[-1]

print(f"\n🖼  Multimodal (image) case:")
print(f"   {PROD_URL}/case/{img_case_id}")
