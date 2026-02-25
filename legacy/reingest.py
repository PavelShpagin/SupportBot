"""
Run re-ingestion for the 105-message group directly on the VM,
bypassing the QR-scan flow entirely.

Steps:
1. Fetch raw messages from DB (already there from previous run)
2. Chunk + LLM-extract case blocks (same as signal-ingest)
3. Archive old cases, clear buffer/reactions, re-insert raw messages
4. Semantic dedup + upsert cases (mirrors _process_history_cases_bg)
5. Print final case list with links

Usage (run inside signal-bot container):
    PYTHONPATH=/app python3 /tmp/reingest.py
"""
import json, os, sys, re, logging
logging.basicConfig(level=logging.WARNING)

from app.config import load_settings
from app.db import create_db, ensure_schema
from app.db.queries_mysql import (
    RawMessage,
    insert_raw_message,
    find_similar_case, merge_case, store_case_embedding,
    upsert_case, new_case_id,
    archive_cases_for_group, clear_group_runtime_data,
    mark_case_in_rag,
)
from app.llm.client import LLMClient
from app.rag.chroma import create_chroma

settings = load_settings()
db       = create_db(settings)
ensure_schema(db)
llm      = LLMClient(settings)
rag      = create_chroma(settings)

# ── pick the group with the most raw messages ─────────────────────────────
with db.connection() as conn:
    cur = conn.cursor()
    cur.execute(
        "SELECT group_id, count(*) as n FROM raw_messages "
        "GROUP BY group_id ORDER BY n DESC LIMIT 1"
    )
    row = cur.fetchone()

if not row:
    print("No raw messages found in DB"); sys.exit(1)

GROUP_ID  = row[0]
MSG_COUNT = row[1]
print(f"Re-ingesting group {GROUP_ID[:40]}... ({MSG_COUNT} messages)")

# ── fetch raw messages WITH all columns (needed for re-insert) ─────────────
with db.connection() as conn:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT ts, sender_hash, sender_name, content_text,
               image_paths_json, message_id
        FROM raw_messages
        WHERE group_id = %s
        ORDER BY ts ASC
        """,
        (GROUP_ID,),
    )
    rows = cur.fetchall()

raw_msg_objects = []
messages_for_ingest = []
for ts, sender_hash, sender_name, content, img_json, mid in rows:
    raw_msg_objects.append(RawMessage(
        message_id=mid,
        group_id=GROUP_ID,
        ts=ts,
        sender_hash=sender_hash,
        sender_name=sender_name,
        content_text=content or "",
        image_paths=json.loads(img_json) if img_json else [],
        reply_to_id=None,
    ))
    if (content or "").strip():
        messages_for_ingest.append({
            "ts": ts, "sender": sender_hash,
            "body": content or "", "id": mid, "reactions": 0
        })

print(f"  Loaded {len(messages_for_ingest)} non-empty messages")

# ── chunk + extract case blocks ───────────────────────────────────────────
def _chunk(msgs, max_chars=12000, overlap=3):
    formatted = []
    for m in msgs:
        text = m.get("body") or ""
        if not text.strip():
            continue
        sender   = m.get("sender", "unknown")
        ts       = m.get("ts", 0)
        msg_id   = m.get("id", str(ts))
        reactions = int(m.get("reactions") or 0)
        header = f'{sender} ts={ts} msg_id={msg_id}'
        if reactions > 0:
            header += f' reactions={reactions}'
        formatted.append(f'{header}\n{text}\n')
    chunks, cur_chunk = [], []
    for line in formatted:
        if len("".join(cur_chunk) + line) > max_chars and cur_chunk:
            chunks.append("".join(cur_chunk))
            cur_chunk = cur_chunk[-overlap:] if overlap > 0 else []
        cur_chunk.append(line)
    if cur_chunk:
        chunks.append("".join(cur_chunk))
    return chunks

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
- Preserve the original message headers (sender_hash ts=... msg_id=...) verbatim inside case_block.
- Do not paraphrase or summarize; copy the exact message lines.
- If there are no solved cases, return {"cases": []}.

Resolution signals (from strongest to weakest):
- reactions=N (N > 0) on a technical answer message -- STRONG signal, treat as confirmed resolved
- Text confirmation after a technical answer (any language):
  English: "thanks", "working", "works", "ok", "solved", "it worked", "fixed"
  Ukrainian: "дякую", "працює", "вирішено", "ок", "заробило"
  Russian: "спасибо", "заработало", "помогло"
- The conversation thread ends after a technical answer (no follow-up questions)

Be generous: if a technical answer has any positive reaction OR brief confirmation, treat as solved.
"""

def _extract_blocks(chunk_text):
    resp = llm.client.chat.completions.create(
        model=settings.model_blocks,
        messages=[
            {"role": "system", "content": P_BLOCKS_SYSTEM},
            {"role": "user",   "content": f"HISTORY_CHUNK:\n{chunk_text}"},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    raw  = resp.choices[0].message.content or "{}"
    data = json.loads(raw)
    out  = []
    for c in data.get("cases", []):
        if isinstance(c, dict) and isinstance(c.get("case_block"), str) and c["case_block"].strip():
            out.append(c["case_block"].strip())
    return out

chunks = _chunk(messages_for_ingest)
print(f"  Chunks: {len(chunks)}")

raw_blocks, seen = [], set()
for i, chunk in enumerate(chunks):
    print(f"  Extracting chunk {i+1}/{len(chunks)}...", flush=True)
    blocks = _extract_blocks(chunk)
    for b in blocks:
        key = b[:120]
        if key not in seen:
            seen.add(key)
            raw_blocks.append(b)

print(f"  Raw blocks (deduplicated): {len(raw_blocks)}")

# ── archive old cases, clear runtime data, re-insert messages ────────────
archived = archive_cases_for_group(db, GROUP_ID)
print(f"  Archived {archived} old cases")
try:
    rag.delete_cases_by_group(GROUP_ID)
except Exception:
    pass

# Clear buffer + reactions (NOT raw_messages yet — we need to re-insert them)
with db.connection() as conn:
    cur = conn.cursor()
    cur.execute("DELETE FROM buffers   WHERE group_id = %s", (GROUP_ID,))
    cur.execute("DELETE FROM reactions WHERE group_id = %s", (GROUP_ID,))
    cur.execute("DELETE FROM raw_messages WHERE group_id = %s", (GROUP_ID,))
    conn.commit()

# Re-insert all raw messages so evidence links resolve
restored = 0
for msg in raw_msg_objects:
    if insert_raw_message(db, msg):
        restored += 1
print(f"  Re-inserted {restored} raw messages")

# ── build message_lookup for evidence matching ────────────────────────────
message_lookup = {}
for msg in raw_msg_objects:
    if msg.content_text:
        message_lookup[msg.content_text.strip()[:100]] = msg.message_id
    message_lookup[str(msg.ts)] = msg.message_id

# ── structure + semantic-dedup + insert ───────────────────────────────────
inserted = merged = skipped = 0
for i, block in enumerate(raw_blocks):
    print(f"  Structuring block {i+1}/{len(raw_blocks)}...", flush=True)
    case = llm.make_case(case_block_text=block)
    if not case.keep:
        skipped += 1
        continue

    # evidence ids
    evidence_ids = list(case.evidence_ids)
    if not evidence_ids:
        for line in block.split('\n'):
            line = line.strip()
            if not line:
                continue
            m2 = re.search(r'msg_id=(\S+)', line)
            if m2:
                eid = m2.group(1)
                if eid not in evidence_ids:
                    evidence_ids.append(eid)
            ts_m = re.search(r'ts=(\d+)', line)
            if ts_m:
                mid = message_lookup.get(ts_m.group(1))
                if mid and mid not in evidence_ids:
                    evidence_ids.append(mid)

    embed_text      = f"{case.problem_title}\n{case.problem_summary}"
    dedup_embedding = llm.embed(text=embed_text)
    similar_id      = find_similar_case(db, group_id=GROUP_ID, embedding=dedup_embedding)

    if similar_id:
        merge_case(
            db,
            target_case_id=similar_id,
            status=case.status,
            problem_summary=case.problem_summary,
            solution_summary=case.solution_summary or "",
            tags=case.tags,
            evidence_ids=evidence_ids,
            evidence_image_paths=[],
        )
        store_case_embedding(db, similar_id, dedup_embedding)
        merged += 1
        final_case_id = similar_id
    else:
        cid = new_case_id(db)
        final_case_id, created = upsert_case(
            db,
            case_id=cid,
            group_id=GROUP_ID,
            status=case.status,
            problem_title=case.problem_title,
            problem_summary=case.problem_summary,
            solution_summary=case.solution_summary or "",
            tags=case.tags,
            evidence_ids=evidence_ids,
            evidence_image_paths=[],
        )
        store_case_embedding(db, final_case_id, dedup_embedding)
        inserted += 1

    # Index solved cases in SCRAG
    if case.status == "solved" and (case.solution_summary or "").strip():
        doc_text = "\n".join([
            f"[SOLVED] {case.problem_title.strip()}",
            f"Проблема: {case.problem_summary.strip()}",
            f"Рішення: {case.solution_summary.strip()}",
            "tags: " + ", ".join(case.tags),
        ]).strip()
        rag_emb = llm.embed(text=doc_text)
        rag.upsert_case(case_id=final_case_id, document=doc_text, embedding=rag_emb,
                        metadata={"group_id": GROUP_ID, "status": "solved"})
        mark_case_in_rag(db, final_case_id)

# ── final report ──────────────────────────────────────────────────────────
with db.connection() as conn:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.case_id, c.status, c.closed_emoji, c.problem_title,
               COUNT(ce.message_id) as evidence_count
        FROM cases c
        LEFT JOIN case_evidence ce ON ce.case_id = c.case_id
        WHERE c.group_id = %s AND c.status != 'archived'
        GROUP BY c.case_id
        ORDER BY c.created_at
        """,
        (GROUP_ID,),
    )
    final = cur.fetchall()

print()
print("=" * 65)
print("RE-INGESTION COMPLETE")
print(f"  Raw blocks : {len(raw_blocks)}")
print(f"  Skipped    : {skipped}")
print(f"  Inserted   : {inserted}")
print(f"  Merged     : {merged}")
print(f"  FINAL      : {len(final)} unique case(s)")
print("=" * 65)
for cid, status, emoji, title, ev_count in final:
    confirmed = f" [{emoji}]" if emoji and emoji != 'NULL' else ""
    print(f"  [{status}{confirmed}] ({ev_count} evidence msgs)")
    print(f"    {title}")
    print(f"    https://supportbot.info/case/{cid}")
print("=" * 65)
