"""
Remote reingest: trigger full re-ingestion via the production API without SSH.

Steps:
1. Create a history token via /history/token (debug endpoint)
2. Extract case blocks from the fixture using the LLM (local)
3. POST to /history/cases with token + messages + blocks
   → production _process_history_cases_bg archives old cases,
     clears raw_messages, re-inserts them, creates cases with evidence

Usage:
    GOOGLE_API_KEY=<key> python3 scripts/remote_reingest.py
"""
import json, os, re, sys, uuid
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "signal-ingest"))
sys.path.insert(0, str(ROOT / "signal-bot"))

API_BASE = os.getenv("API_BASE", "https://supportbot.info")
FIXTURE  = ROOT / "tests" / "fixtures" / "sample_chat.json"

# ── 0. Check API key ──────────────────────────────────────────────────────────
api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("OPENAI_API_KEY")
if not api_key:
    print("ERROR: GOOGLE_API_KEY not set"); sys.exit(1)

# ── 1. Load fixture ───────────────────────────────────────────────────────────
print(f"Loading fixture: {FIXTURE}")
data     = json.loads(FIXTURE.read_text(encoding="utf-8"))
messages = data["messages"]
GROUP_ID = data["group_id"]
print(f"  {len(messages)} messages, group_id={GROUP_ID[:40]}...")

# ── 2. Create token via debug endpoint ────────────────────────────────────────
import urllib.request, urllib.error

def api_post(path, body):
    url = f"{API_BASE}{path}"
    payload = json.dumps(body).encode()
    req = urllib.request.Request(url, data=payload,
                                  headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()[:400]
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {body_text}")

print("\nCreating history token...")
tok_resp = api_post("/history/token", {
    "admin_id": "reingest_admin",
    "group_id": GROUP_ID,
})
token = tok_resp["token"]
print(f"  Token: {token}")

# ── 3. Extract case blocks (LLM) ──────────────────────────────────────────────
from openai import OpenAI

oc = OpenAI(api_key=api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/")

MODEL = os.getenv("MODEL_BLOCKS", "gemini-2.0-flash")

P_BLOCKS_SYSTEM = """You analyze a chunk of support chat history and extract FULLY RESOLVED support cases.

Each message in the chunk is formatted as:
  sender_hash ts=TIMESTAMP msg_id=MESSAGE_ID
  message text

Return ONLY valid JSON with key:
- cases: array of objects, each with:
  - case_block: string (the EXACT messages from the chunk that form this case, problem through resolution, preserving all header lines with msg_id)

Rules:
- Extract ONLY solved cases with a HUMAN-CONFIRMED working solution.
- Do NOT extract open/unresolved issues, greetings, or off-topic messages.
- Each case_block must include both the problem AND a human confirmation that it was resolved.
- Preserve the original message headers (sender_hash ts=... msg_id=...) verbatim inside case_block.
- Do not paraphrase or summarize; copy the exact message lines.
- If there are no solved cases, return {"cases": []}.

CRITICAL — valid resolution signals (at least one REQUIRED):
- reactions=N (N > 0) on a message — STRONGEST signal, always treat as confirmed resolved
- Explicit human text confirmation AFTER a technical answer:
  English: "thanks", "working", "works", "ok", "solved", "it worked", "fixed"
  Ukrainian: "дякую", "працює", "вирішено", "ок", "заробило", "допомогло"
  Russian: "спасибо", "заработало", "помогло"

CRITICAL — what does NOT count as resolution:
- Silence or end of thread — a question with no follow-up is NOT solved
- A person answering their own question without the questioner confirming it worked
- One person suggesting a solution with no acknowledgement from the person who had the problem
"""

def _is_bot_message(body: str) -> bool:
    return "supportbot.info/case/" in (body or "")

def _chunk_messages(msgs, max_chars=12000, overlap=3):
    formatted = []
    for m in msgs:
        body = (m.get("body") or "").strip()
        if not body:
            continue
        if _is_bot_message(body):
            continue  # Never include bot auto-responses in extraction
        sender   = m.get("sender", "?")
        ts       = m.get("ts", 0)
        mid      = m.get("id", str(ts))
        reactions = int(m.get("reactions") or 0)
        header = f"{sender} ts={ts} msg_id={mid}"
        if reactions > 0:
            header += f" reactions={reactions}"
            rxn_emoji = m.get("reaction_emoji") or ""
            if rxn_emoji:
                header += f" reaction_emoji={rxn_emoji}"
        formatted.append(f"{header}\n{body}\n")
    chunks, cur_chunk = [], []
    for line in formatted:
        if len("".join(cur_chunk) + line) > max_chars and cur_chunk:
            chunks.append("".join(cur_chunk))
            cur_chunk = cur_chunk[-overlap:] if overlap > 0 else []
        cur_chunk.append(line)
    if cur_chunk:
        chunks.append("".join(cur_chunk))
    return chunks

def _extract_blocks(chunk_text):
    resp = oc.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": P_BLOCKS_SYSTEM},
            {"role": "user",   "content": f"HISTORY_CHUNK:\n{chunk_text}"},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    raw  = resp.choices[0].message.content or "{}"
    data = json.loads(raw)
    return [c["case_block"].strip() for c in data.get("cases", [])
            if isinstance(c, dict) and c.get("case_block", "").strip()]

chunks = _chunk_messages(messages)
print(f"\nChunking: {len(messages)} messages → {len(chunks)} chunk(s)")
raw_blocks, seen = [], set()
for i, chunk in enumerate(chunks):
    print(f"  Extracting chunk {i+1}/{len(chunks)}...", flush=True)
    for b in _extract_blocks(chunk):
        key = b[:120]
        if key not in seen:
            seen.add(key)
            raw_blocks.append(b)
print(f"  Raw blocks (deduplicated): {len(raw_blocks)}")

# ── 4. Build history messages payload (exclude bot messages from evidence) ───
hist_messages = []
for m in messages:
    mid  = m.get("id") or str(uuid.uuid4())
    body = m.get("body") or ""
    if _is_bot_message(body):
        continue  # Bot messages must not appear in case evidence
    hist_messages.append({
        "message_id":    mid,
        "sender_hash":   m.get("sender") or "unknown",
        "sender_name":   None,
        "ts":            int(m.get("ts", 0)),
        "content_text":  body,
        "image_payloads": [],
    })

# ── 5. POST /history/cases ────────────────────────────────────────────────────
print(f"\nPOSTing /history/cases: {len(raw_blocks)} blocks, {len(hist_messages)} messages...")
try:
    result = api_post("/history/cases", {
        "token":    token,
        "group_id": GROUP_ID,
        "cases":    [{"case_block": b} for b in raw_blocks],
        "messages": hist_messages,
    })
    print(f"  Result: {result}")
except RuntimeError as e:
    print(f"  ERROR: {e}")
    sys.exit(1)

# ── 6. Fetch and display final cases ─────────────────────────────────────────
print(f"\ncases_inserted: {result.get('cases_inserted', '?')}")
case_ids = result.get("case_ids", [])

if case_ids:
    print(f"\nFetching {len(case_ids)} new case details...")
    print("=" * 65)
    for cid in case_ids:
        try:
            resp = urllib.request.urlopen(
                f"{API_BASE}/api/cases/{cid}", timeout=15
            )
            data = json.loads(resp.read())
            evidence = data.get("evidence", [])
            emoji = f" [{data['closed_emoji']}]" if data.get("closed_emoji") else ""
            print(f"  [{data['status']}{emoji}] {data['problem_title']}")
            print(f"  Evidence: {len(evidence)} msgs")
            print(f"  https://supportbot.info/case/{cid}")
            print()
        except Exception as e:
            print(f"  ERROR for {cid}: {e}")
    print("=" * 65)
else:
    # Try the new group cases endpoint (available after deploying this commit)
    print(f"\nNo case_ids returned (old server). Trying GET /api/group-cases...")
    import urllib.parse
    encoded_gid = urllib.parse.quote(GROUP_ID, safe="")
    try:
        resp = urllib.request.urlopen(
            f"{API_BASE}/api/group-cases?group_id={encoded_gid}", timeout=15
        )
        data = json.loads(resp.read())
        cases = data.get("cases", [])
        print(f"Found {len(cases)} active cases for this group:")
        print("=" * 65)
        for c in cases:
            cid = c["case_id"]
            emoji = f" [{c['closed_emoji']}]" if c.get("closed_emoji") else ""
            print(f"  [{c['status']}{emoji}] {c['problem_title']}")
            print(f"  https://supportbot.info/case/{cid}")
            print()
        print("=" * 65)
    except Exception as e:
        print(f"  ERROR: {e} — deploy the latest code first")
