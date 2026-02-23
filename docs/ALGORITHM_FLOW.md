# SupportBot — Algorithm & Architecture (Full Technical Reference)

**Last Updated**: 2026-02-23  
**Status**: Current & Accurate (reflects production code)

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Services & Components](#2-services--components)
3. [Data Stores](#3-data-stores)
4. [Live Message Pipeline](#4-live-message-pipeline)
5. [Case Extraction Pipeline (BUFFER_UPDATE)](#5-case-extraction-pipeline-buffer_update)
6. [Answer Pipeline (MAYBE_RESPOND)](#6-answer-pipeline-maybe_respond)
7. [Emoji Reaction & Case Confirmation](#7-emoji-reaction--case-confirmation)
8. [History Ingestion (signal-ingest)](#8-history-ingestion-signal-ingest)
9. [Answer Engine Context Layers (SCRAG / B3 / B1)](#9-answer-engine-context-layers-scrag--b3--b1)
10. [LLM Calls Reference](#10-llm-calls-reference)
11. [Worker Maintenance Tasks](#11-worker-maintenance-tasks)
12. [Case Lifecycle Summary](#12-case-lifecycle-summary)
13. [Configuration Parameters](#13-configuration-parameters)
14. [Error Handling Patterns](#14-error-handling-patterns)

---

## 1. System Overview

```
┌───────────────────────────────────────────────────────────────────────────┐
│                         SIGNAL GROUP CHAT                                  │
│  Users send messages, images, emoji reactions to a Signal support group    │
└──────────────────────────────┬────────────────────────────────────────────┘
                               │ Messages / Reactions
                               ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                      signal-desktop (headless)                            │
│  - Runs Signal Desktop in headless mode with SQLite DB (SQLCipher)        │
│  - Exposes HTTP API: /group/messages, /group/send, /reactions, etc.       │
│  - Polls for new messages and reactions                                   │
└──────────────────────────────┬───────────────────────────────────────────┘
                               │ HTTP
                               ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                        signal-bot (FastAPI)                               │
│  ┌─────────────────┐  ┌────────────────────┐  ┌──────────────────────┐  │
│  │ Ingest Layer    │  │  Worker (2 queues)  │  │  HTTP API (web)      │  │
│  │                 │  │                    │  │                      │  │
│  │ ingest_message  │  │  BUFFER_UPDATE      │  │  /case/{id}          │  │
│  │ _handle_react.  │  │  MAYBE_RESPOND      │  │  /history/cases      │  │
│  └────────┬────────┘  └─────────┬──────────┘  └──────────────────────┘  │
│           │                     │                                         │
│           ▼                     ▼                                         │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  MySQL Database                                                     │  │
│  │  raw_messages · buffers · cases · reactions · jobs · admin_sessions │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│           │                     │                                         │
│           ▼                     ▼                                         │
│  ┌──────────────────┐  ┌────────────────────────┐                        │
│  │  ChromaDB (SCRAG)│  │  Gemini API (LLM)       │                        │
│  │  Vector store of │  │  - gemini-2.0-flash      │                        │
│  │  solved cases    │  │  - gemini-embedding-001   │                        │
│  └──────────────────┘  └────────────────────────┘                        │
└──────────────────────────────────────────────────────────────────────────┘
         ▲
         │  HTTP POST /history/cases
┌────────┴─────────────────────────────────────────────────────────────────┐
│                       signal-ingest                                       │
│  History ingestion service:                                               │
│  - Triggers QR-code linking of admin's Signal account                    │
│  - Reads 45-day chat history from signal-desktop                         │
│  - Extracts solved cases with LLM                                         │
│  - Posts cases to signal-bot                                              │
└──────────────────────────────────────────────────────────────────────────┘
         ▲
         │  Browser / signal-web
┌────────┴─────────────────────────────────────────────────────────────────┐
│                      signal-web (Next.js)                                 │
│  Public web UI for viewing case details, chat history, emoji confirmations│
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Services & Components

| Service | Technology | Role |
|---------|-----------|------|
| `signal-desktop` | Python FastAPI + SQLCipher | Reads/writes Signal Desktop SQLite DB; exposes HTTP API for messages and reactions |
| `signal-bot` | Python FastAPI | Core backend: ingest, worker queues, LLM orchestration, case DB, RAG |
| `signal-ingest` | Python | History import: QR-link admin account, bulk-extract cases from past messages |
| `signal-web` | Next.js (React) | Case viewer web app; displays case details, chat history, confirmation emoji |
| MySQL | MySQL 8 | Primary persistent store: messages, buffers, cases, jobs, sessions |
| ChromaDB | Chroma | Vector store for semantic search over solved cases (SCRAG layer) |
| Gemini API | Google | All LLM calls: gating, case extraction, embedding, answer synthesis |

### Key source files

```
signal-bot/app/
├── main.py                  ← FastAPI app, signal listener, reaction handler
├── ingestion.py             ← ingest_message(): store + enqueue jobs
├── jobs/worker.py           ← BUFFER_UPDATE and MAYBE_RESPOND job handlers
├── agent/
│   ├── ultimate_agent.py    ← UltimateAgent: gate → search → synthesize
│   └── case_search_agent.py ← CaseSearchAgent: SCRAG + B3 + B1 retrieval
├── llm/
│   ├── client.py            ← LLMClient: all Gemini API calls
│   ├── prompts.py           ← All system prompts (P_BLOCKS_SYSTEM, etc.)
│   └── schemas.py           ← Pydantic output schemas
├── db/
│   ├── queries_mysql.py     ← All SQL queries
│   └── schema_mysql.py      ← DB schema (create tables)
└── rag/chroma.py            ← ChromaDB wrapper (SCRAG)

signal-ingest/ingest/main.py ← History ingestion pipeline
signal-desktop/app/
├── db_reader.py             ← Reads Signal Desktop SQLite (SQLCipher)
└── main.py                  ← FastAPI HTTP API over db_reader
```

---

## 3. Data Stores

### MySQL Tables

| Table | Purpose |
|-------|---------|
| `raw_messages` | Every ingested message: `message_id`, `group_id`, `ts`, `sender_hash`, `content_text` (with OCR'd image JSON), `image_paths`, `reply_to_id` |
| `buffers` | Per-group rolling message buffer (plain text, used for LLM case extraction) |
| `cases` | All cases: `case_id`, `group_id`, `status` (open/solved/archived), `problem_title`, `problem_summary`, `solution_summary`, `tags`, `evidence_ids`, `embedding`, `in_rag`, `closed_emoji` |
| `reactions` | Emoji reactions: `group_id`, `target_ts`, `target_author`, `sender_hash`, `emoji` |
| `jobs` | Worker job queue: `job_id`, `type` (BUFFER_UPDATE/MAYBE_RESPOND/HISTORY_LINK), `payload`, `status`, `attempts` |
| `admin_sessions` | Linked admin accounts: `admin_id` (phone), `group_id`, `lang` |
| `history_tokens` | One-time tokens for history import authorization |
| `group_docs` | Optional documentation URLs per group (for `/setdocs` command) |

### ChromaDB (SCRAG)

One collection, keyed by `case_id`. Each entry:
- **document**: structured text — `[SOLVED] <title>\nПроблема: ...\nРішення: ...\ntags: ...`
- **embedding**: 768-dim vector from `gemini-embedding-001`
- **metadata**: `{group_id, status, evidence_ids?, evidence_image_paths?}`

SCRAG is the permanent semantic knowledge base. It only contains **solved** cases with a non-empty solution summary.

---

## 4. Live Message Pipeline

Every message from Signal Desktop flows through this path:

```
Signal Desktop polls its SQLite DB every few seconds
        │
        ▼
SignalDesktopAdapter.listen_forever()
  - Gets new group messages → _handle_group_message(m)
  - Gets reactions          → _handle_reaction(r)
  - Gets contact-removed    → _handle_contact_removed(phone)
        │
        ▼ (group message)
ingest_message(settings, db, llm, message_id, group_id, sender, ts, text, image_paths)
        │
        ├─ Image processing (if attachments):
        │     for each image:
        │       llm.image_to_text_json(image_bytes, context_text)
        │         → ImgExtract {observations: List[str], extracted_text: str}
        │       append to content_text:
        │         "\n\n[image]\n{json}"
        │
        ├─ insert_raw_message(db, RawMessage{...})
        │     ← idempotent; skips if message_id already exists
        │
        └─ enqueue_job(db, BUFFER_UPDATE, payload)
           enqueue_job(db, MAYBE_RESPOND, payload)
```

### Image Processing Details

Images attached to Signal messages are processed immediately at ingest time:
- Calls `llm.image_to_text_json(image_bytes, context_text=original_text)` using `P_IMG_SYSTEM` prompt
- Returns structured JSON: `{"observations": [...], "extracted_text": "..."}`
- This JSON is appended to `content_text` in `raw_messages` so all subsequent LLM calls see the OCR output
- Original image bytes are stored on disk at `settings.signal_bot_storage` path

---

## 5. Case Extraction Pipeline (BUFFER_UPDATE)

Triggered for every new message. Purpose: maintain the rolling buffer (B2) and extract new cases.

```
BUFFER_UPDATE job consumed by worker_loop_forever()
        │
        ▼
_handle_buffer_update(deps, payload)
        │
        ├─ Load message from raw_messages
        ├─ Check positive reactions on this message (from reactions table)
        ├─ Mark as [BOT] if sender_hash == bot_sender_hash
        ├─ Append formatted buffer line:
        │     "{sender_hash}[BOT?] ts={ts} msg_id={msg_id} reactions=N\n{content_text}\n\n"
        │
        ├─ Trim buffer:
        │     - Remove messages older than buffer_max_age_hours
        │     - Remove oldest messages if > buffer_max_messages
        │
        ├─ Parse buffer into indexed message blocks (BufferMessageBlock)
        │
        ├─ Filter out [BOT] blocks for extraction input
        │     (bot messages kept in buffer for context but never become cases)
        │
        ├─── PHASE 1: Extract new case spans ──────────────────────────────────
        │
        │   llm.extract_case_from_buffer(numbered_buffer)
        │     [P_EXTRACT_SYSTEM prompt + gemini-2.0-flash]
        │     → ExtractResult {cases: [{start_idx, end_idx}]}
        │
        │   for each span (start_idx → end_idx):
        │     case_block_text = join messages in span
        │
        │     llm.make_case(case_block_text)
        │       [P_CASE_SYSTEM prompt + gemini-2.0-flash]
        │       → CaseResult {keep, status, problem_title, problem_summary,
        │                      solution_summary, tags}
        │
        │     if not case.keep → skip
        │
        │     Semantic dedup:
        │       embed_text = f"{problem_title}\n{problem_summary}"
        │       embedding = llm.embed(embed_text)
        │       similar_id = find_similar_case(db, group_id, embedding)
        │
        │       if similar_id:
        │         merge_case(db, target=similar_id, ...) → update existing
        │       else:
        │         insert_case(db, new case_id, status=open/solved, ...)
        │       store_case_embedding(db, case_id, embedding)
        │
        │     if status == "solved" AND solution not empty:
        │       Build doc_text:
        │         "[SOLVED] {title}\nПроблема: {problem}\nРішення: {solution}\ntags: ..."
        │       rag_embedding = llm.embed(doc_text)
        │       rag.upsert_case(case_id, doc_text, rag_embedding, metadata)
        │       mark_case_in_rag(db, case_id)
        │       accepted_ranges.append(span) ← will be removed from buffer
        │     else:
        │       store as B1 open case, keep messages in buffer
        │
        ├─── PHASE 2: Dynamic B1 Resolution ───────────────────────────────────
        │
        │   open_cases = get_open_cases_for_group(db, group_id)  ← B1
        │
        │   for each b1_case:
        │     resolution = llm.check_case_resolved(
        │         case_title, case_problem, buffer_text=full_buf)
        │       [P_RESOLUTION_SYSTEM prompt + gemini-2.0-flash]
        │       → ResolutionResult {resolved: bool, solution_summary: str}
        │
        │     if resolved AND solution not empty:
        │       Semantic dedup: check for existing solved case
        │         if exists → merge + archive b1_case
        │         else      → update_case_to_solved(db, case_id, solution)
        │       upsert to SCRAG (mark_case_in_rag)
        │
        └─── Update buffer ─────────────────────────────────────────────────────

            Remove message spans that became solved cases (accepted_ranges)
            set_buffer(db, group_id, buffer_new)
```

### Buffer Line Format

```
{sender_hash}[BOT] ts={timestamp_ms} msg_id={uuid} reply_to={uuid} reactions=N
{content_text}

```

- `[BOT]` tag: only for messages from the bot's own phone number
- `reactions=N`: count of positive emoji reactions from the `reactions` table
- `reply_to=`: quoted message ID (from Signal's quote feature)
- `msg_id=`: used by LLM to output `evidence_ids` for case linking

---

## 6. Answer Pipeline (MAYBE_RESPOND)

Triggered for every new message. Purpose: decide if and how to respond.

```
MAYBE_RESPOND job consumed by worker_loop_forever()
        │
        ▼
_handle_maybe_respond(deps, payload)
        │
        ├─ Load message from raw_messages
        ├─ Skip if content_text is empty (system notification)
        │
        ├─ Check group has active linked admins
        │     get_group_admins(db, group_id) → admin phone numbers
        │     for each admin → get_admin_session(db, admin_id)
        │     if no active sessions → STOP (group not configured)
        │
        ├─ Handle /setdocs command (admin-only)
        │     upsert_group_docs(db, group_id, urls)
        │
        ├─── GATE: decide_consider() ─────────────────────────────────────────
        │
        │   context_text = last 9 messages (excluding current)
        │   gate_images  = first 2 attached images (if present)
        │
        │   gate = llm.decide_consider(
        │       message=content_text,
        │       context=context_text,
        │       images=gate_images)
        │     [P_DECISION_SYSTEM prompt + gemini-2.0-flash (fast)]
        │     → DecisionResult {consider: bool, tag: str}
        │
        │   Tags: new_question | ongoing_discussion | statement | noise
        │
        │   if not gate.consider AND not force:
        │     STOP (silent)
        │
        ├─── ULTIMATE AGENT ──────────────────────────────────────────────────
        │
        │   answer = UltimateAgent.answer(
        │       question=content_text, group_id, db, lang)
        │
        │   ┌─ CaseSearchAgent.answer() ──────────────────────────────────┐
        │   │  1. SCRAG: embed query → cosine search ChromaDB (top 3)     │
        │   │  2. B3: get_recent_solved_cases(db, group_id, since_ts)     │
        │   │         (solved cases with evidence still in B2 window)     │
        │   │  3. B1: get_open_cases_for_group(db, group_id)              │
        │   │                                                              │
        │   │  Priority:                                                   │
        │   │    SCRAG or B3 results → return formatted solved context     │
        │   │    Only B1 results    → return "B1_ONLY:<context>"          │
        │   │    Nothing            → return "No relevant cases found."    │
        │   └─────────────────────────────────────────────────────────────┘
        │
        │   Synthesizer (gemini-2.0-flash) builds final answer:
        │
        │   if "No relevant cases found." → answer = "[[TAG_ADMIN]]"
        │
        │   if "B1_ONLY:...":
        │     Prompt: "state the issue is tracked + include case link + [[TAG_ADMIN]]"
        │     → 1-sentence response mentioning open case + admin tag
        │
        │   if solved cases found:
        │     Prompt: "State the ACTUAL solution in 1-2 sentences. Add case link."
        │             "If retrieved cases don't address question → [[TAG_ADMIN]]"
        │             "If user must provide something → add [[TAG_ADMIN]] + link"
        │     → direct answer + case link
        │
        ├─── SEND ────────────────────────────────────────────────────────────
        │
        │   [[TAG_ADMIN]] → replace with @mention of active admins
        │
        │   signal.send_group_text(
        │       group_id=group_id,
        │       text=answer,
        │       quote_timestamp=original_ts,   ← bot replies quoting the user
        │       quote_author=sender,
        │       quote_message=original_text,
        │       mention_recipients=admin_phones)
        │
        └──────────────────────────────────────────────────────────────────────
```

### Gate Prompt (P_DECISION_SYSTEM)

The gate model decides `consider=true/false` and classifies the message:

| Tag | Meaning | consider |
|-----|---------|----------|
| `new_question` | New support question, no related context | **true** |
| `ongoing_discussion` | Continues an active thread in context | **true** |
| `statement` | Summary / conclusion / "I solved it" without asking for help | **false** |
| `noise` | Greeting, "ok", emoji-only, off-topic | **false** |

Key rules:
- `consider=true` for technical problem descriptions (even if phrased as statements) — these are captured by BUFFER_UPDATE, not MAYBE_RESPOND
- `consider=false` for summaries that start with "Підсумовуючи", "Резюмуючи" etc.
- Bot mention (`force=true`) bypasses the gate

---

## 7. Emoji Reaction & Case Confirmation

Emoji reactions are a primary signal for confirming a case was solved.

```
Signal Desktop receives emoji reaction
        │
        ▼
_handle_reaction(r: InboundReaction)
        │
        ├─ Hash sender: sender_h = hash_sender(r.sender)
        │
        ├─ if r.is_remove:
        │     delete_reaction(db, group_id, target_ts, sender_h, emoji)
        │
        └─ else:
              upsert_reaction(db, group_id, target_ts, target_author, sender_h, emoji)
              log "Reaction added"
              
              if r.emoji in POSITIVE_EMOJI:
                n = confirm_cases_by_evidence_ts(
                    db, group_id=r.group_id, target_ts=r.target_ts, emoji=r.emoji)
                
                if n > 0:
                  log "Case confirmation via emoji {emoji} on ts={ts}: {n} cases confirmed"
```

### POSITIVE_EMOJI Set

Defined in `app/db/__init__.py` (MySQL module). Includes thumbs up, heart, checkmark, and other approval emoji variants across Unicode code points.

### confirm_cases_by_evidence_ts()

SQL logic: find all `cases` where `evidence_ids` JSON array contains any message with timestamp `target_ts` in `raw_messages`, then:
- Update `status = 'solved'`
- Set `closed_emoji = r.emoji` (the actual emoji used, e.g. "🫡", "+", "👍")

This is also triggered from history ingestion when `reactions=N` is present in the chunk.

### closed_emoji Display (signal-web)

The `closed_emoji` field is stored in the `cases` table and displayed in the case page chat history:

```html
{data.closed_emoji && data.status === 'solved' && (
  <div className="emoji-confirmation">
    <span className="emoji-bubble">{data.closed_emoji}</span>
    Учасник підтвердив вирішення реакцією
  </div>
)}
```

This appears inside the chat history section (not the page header), showing the actual emoji the participant used.

---

## 8. History Ingestion (signal-ingest)

Used to backfill the knowledge base from past Signal chat history.

```
Admin initiates history import (via signal-web or API)
        │
        ▼
signal-bot: POST /history/link-token
  → creates one-time token + HISTORY_LINK job
  → sends DM to admin with QR link

HISTORY_LINK job picked up by worker:
  → POST signal-ingest/jobs   {admin_id, group_id, token, lang}
  → signal-ingest starts job

signal-ingest job flow:
        │
        ├─ 1. Reset Signal Desktop (clear previous account)
        │     POST signal-desktop/reset
        │
        ├─ 2. Request new QR code
        │     POST signal-desktop/link-account
        │     → returns QR code as base64 PNG
        │
        ├─ 3. Send QR image to admin via signal-bot
        │     POST signal-bot/history/qr-ready  {token, qr_base64}
        │     → signal-bot sends DM with QR to admin
        │
        ├─ 4. Wait for admin to scan QR (links their account to signal-desktop)
        │     Poll signal-desktop/status until linked (timeout: 5 min)
        │
        ├─ 5. Fetch historical messages from signal-desktop
        │     GET signal-desktop/group/{group_id}/messages
        │     → returns list of SignalMessage {ts, sender, text, reactions, reaction_emoji, ...}
        │
        ├─ 6. Chunk messages and extract cases with LLM
        │
        │   _chunk_messages(messages, bot_e164):
        │     - Skip bot messages (_is_bot_message: checks sender == bot_e164
        │                          or "supportbot.info/case/" in text)
        │     - Format each message header:
        │         "{sender_hash} ts={ts} msg_id={msg_id}
        │          reactions={N} reaction_emoji={emoji}"
        │     - Split into overlapping chunks of ~150 messages
        │
        │   For each chunk:
        │     LLM (P_BLOCKS_SYSTEM prompt) → {cases: [{case_block: str}]}
        │
        │     P_BLOCKS_SYSTEM resolution signals:
        │       STRONG: reactions=N (N>0) on a technical answer
        │       MEDIUM: text confirmation ("дякую", "працює", "ok", etc.)
        │       WEAK:   conversation ends after technical answer
        │       NOTE:   "thread ends" is intentionally kept as a weak signal;
        │               bot replies are filtered out before LLM sees the chunk
        │
        ├─ 7. Post extracted cases to signal-bot
        │     POST signal-bot/history/cases
        │       {token, cases: [{case_block, reaction_emoji?}]}
        │
        │     signal-bot _process_history_cases_bg():
        │       for each case_block:
        │         1. Parse evidence_ids from msg_id= headers
        │         2. llm.make_case(case_block) → CaseResult
        │         3. Semantic dedup: find_similar_case() → merge or insert
        │         4. If emoji_confirmed (reactions=N in block):
        │              extract reaction_emoji from "reaction_emoji=X" in block
        │              UPDATE cases SET closed_emoji=X WHERE case_id=...
        │         5. If solved: upsert to SCRAG
        │
        └─ 8. Reset Signal Desktop again (remove admin's account)
              POST signal-desktop/reset
              → Privacy: admin's account is unlinked immediately after import
```

### History Case Extraction Prompt (P_BLOCKS_SYSTEM)

```
Analyze chunk of support chat history → extract FULLY RESOLVED cases.

Message format: sender_hash ts=TIMESTAMP msg_id=MESSAGE_ID\nmessage text

Resolution signals (strongest → weakest):
  1. reactions=N (N>0) on technical answer   ← STRONG, treat as confirmed
  2. Text confirmation after technical answer
     ("дякую", "працює", "ok", "working", "thanks", etc.)
  3. Thread ends after technical answer      ← WEAK signal

Rules:
  - Extract ONLY solved cases (problem + confirmed solution)
  - Do NOT extract open/unresolved, greetings, off-topic
  - Preserve original message headers verbatim (needed for evidence_ids)
  - Bot messages are pre-filtered; never appear in the chunk input
  - Return {"cases": []} if no solved cases found
```

---

## 9. Answer Engine Context Layers (SCRAG / B3 / B1)

When answering a user question, the bot queries three context layers:

```
┌─────────────────────────────────────────────────────────────────┐
│  SCRAG — Solved Cases RAG (ChromaDB, permanent)                 │
│  ├─ Source: solved cases with non-empty solution summary        │
│  ├─ Indexed: immediately when a case is marked solved           │
│  ├─ Search: cosine similarity (gemini-embedding-001, 768-dim)   │
│  ├─ Filter: by group_id (each group has its own knowledge base) │
│  └─ Top-K: 3 results returned                                   │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  B3 — Recently Solved Buffer (MySQL query)                      │
│  ├─ Source: solved cases whose evidence_ts falls in B2 window   │
│  ├─ Query: get_recent_solved_cases(db, group_id, since_ts)      │
│  └─ Purpose: catches cases solved in the last few days          │
│     before embedding had time to matter / before full SCRAG sync│
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  B1 — Open Cases (MySQL query)                                  │
│  ├─ Source: cases WHERE status='open' AND group_id=?            │
│  ├─ Expiry: auto-deleted after 7 days (hourly B1 expiry job)    │
│  └─ Use: tell user the issue is tracked, tag admin              │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  B2 — Rolling Message Buffer (MySQL buffers table)              │
│  ├─ Content: all recent group messages as formatted text        │
│  ├─ Age limit: buffer_max_age_hours (configurable)              │
│  ├─ Size limit: buffer_max_messages (configurable)              │
│  └─ Use: case extraction input (BUFFER_UPDATE) and B1 check     │
└─────────────────────────────────────────────────────────────────┘
```

### Response Decision Tree

```
CaseSearchAgent.answer(question, group_id, db)
        │
        ├─ SCRAG search (top 3) + B3 lookup
        │
        ├─ Any solved results (SCRAG or B3)?
        │     YES → format context: problem + solution + case link
        │           → UltimateAgent synthesizer generates direct answer
        │
        ├─ No solved results. Any B1 (open) cases?
        │     YES → format: "B1_ONLY:{open case context}"
        │           → synthesizer generates 1 sentence: "being tracked" + link + [[TAG_ADMIN]]
        │
        └─ Nothing at all → "No relevant cases found."
              → UltimateAgent returns "[[TAG_ADMIN]]"
              → Worker replaces with @mention of active group admins
```

---

## 10. LLM Calls Reference

All calls use Gemini API via OpenAI-compatible endpoint. Models:

| Call | Function | Model | Purpose | Output Schema |
|------|----------|-------|---------|---------------|
| Image OCR | `llm.image_to_text_json()` | gemini-2.0-flash | Extract text & observations from image | `ImgExtract {observations, extracted_text}` |
| Gate | `llm.decide_consider()` | gemini-2.0-flash | Filter noise / classify message | `DecisionResult {consider, tag}` |
| Case extract | `llm.extract_case_from_buffer()` | gemini-2.0-flash | Find case spans in numbered buffer | `ExtractResult {cases: [{start_idx, end_idx}]}` |
| Case structure | `llm.make_case()` | gemini-2.0-flash | Structure a case block into fields | `CaseResult {keep, status, problem_title, problem_summary, solution_summary, tags}` |
| B1 resolution | `llm.check_case_resolved()` | gemini-2.0-flash | Check if open case resolved by new buffer | `ResolutionResult {resolved, solution_summary}` |
| Embed | `llm.embed()` | gemini-embedding-001 | 768-dim vector for dedup + SCRAG search | `List[float]` |
| Synthesize | `synthesizer.generate_content()` | gemini-2.0-flash | Final user-facing answer | Free text |
| History extract | P_BLOCKS_SYSTEM prompt | gemini-2.0-flash (via OpenAI client) | Extract solved cases from history chunk | `{cases: [{case_block: str}]}` |

### Embedding & Deduplication

Every case is embedded twice:
1. **Dedup embed**: `"{problem_title}\n{problem_summary}"` — used by `find_similar_case()` to prevent duplicate cases for the same problem
2. **SCRAG embed**: full `doc_text` (`[SOLVED] title\nПроблема: ...\nРішення: ...\ntags: ...`) — used for semantic search at answer time

`find_similar_case()` uses a cosine similarity threshold (configurable) to decide if two cases are "the same problem". If a match is found, `merge_case()` updates the existing case rather than creating a new one.

---

## 11. Worker Maintenance Tasks

The worker loop runs two periodic maintenance tasks:

### B1 Expiry (hourly)
```python
expire_old_open_cases(db, max_age_days=7)
```
Open cases older than 7 days are deleted (the problem was never resolved or is stale).

### SCRAG Sync (hourly)
```python
_run_sync_rag(deps)
```
Compares ChromaDB entries against MySQL active case IDs. Removes stale ChromaDB entries whose MySQL case no longer exists (e.g. was archived or deleted). This is the authoritative reconciliation — keeps SCRAG consistent without per-query MySQL lookups.

---

## 12. Case Lifecycle Summary

```
MESSAGE ARRIVES
       │
       ▼
raw_messages: inserted (idempotent)
       │
       ├── BUFFER_UPDATE: added to B2 buffer
       │         │
       │         ├── LLM: extract_case_from_buffer()
       │         │         │
       │         │         ├── make_case() → status=open  → B1 (cases table, in_rag=0)
       │         │         │
       │         │         └── make_case() → status=solved → SCRAG + B3 + remove from B2
       │         │
       │         └── For each B1 case: check_case_resolved()
       │                   │
       │                   └── resolved=true → promote to solved → SCRAG + B3
       │
       └── MAYBE_RESPOND: gate → search (SCRAG+B3+B1) → synthesize → send

EMOJI REACTION
       │
       └── upsert_reaction → confirm_cases_by_evidence_ts()
                 → UPDATE cases SET status=solved, closed_emoji=emoji

HISTORY IMPORT
       │
       └── signal-ingest: LLM extracts from history chunks
                 → POST /history/cases
                 → make_case() → insert/merge → SCRAG (if solved)
                 → closed_emoji set from reaction_emoji in chunk headers

CASE VIEWED
       │
       └── GET /api/case/{id} → MySQL → signal-web renders:
                 - problem title / summary
                 - solution summary
                 - full chat history (with timestamps)
                 - closed_emoji banner (in chat history)
```

---

## 13. Configuration Parameters

Key settings from `settings` (loaded from environment / `.env`):

| Setting | Default | Description |
|---------|---------|-------------|
| `buffer_max_age_hours` | 72 | B2 buffer: drop messages older than N hours |
| `buffer_max_messages` | 200 | B2 buffer: maximum message count |
| `worker_poll_seconds` | 1 | Job queue poll interval |
| `signal_bot_e164` | — | Bot's own phone number (for bot message detection) |
| `signal_bot_storage` | — | Path to Signal storage (images) |
| `signal_desktop_url` | — | signal-desktop HTTP API base URL |
| `use_signal_desktop` | false | Use Signal Desktop adapter vs signal-cli |
| `public_url` | — | Base URL for case links (e.g. `https://supportbot.info`) |
| `bot_mention_strings` | — | List of strings that trigger forced response |
| `max_image_size_bytes` | — | Skip images larger than this |
| `openai_api_key` | — | Google API key (used with OpenAI-compat endpoint) |

---

## 14. Error Handling Patterns

### Idempotency
- `insert_raw_message`: skips duplicate `message_id` (INSERT IGNORE)
- `upsert_case`: on conflict, updates existing case
- `rag.upsert_case`: Chroma upsert replaces existing entry

### Worker Retries
- Failed jobs are retried up to 3 times (`fail_job` increments `attempts`)
- After 3 failures, job is permanently marked failed

### Signal Adapter Fallbacks
- Signal Desktop not available at boot → listener started lazily on first health check
- `send_direct_text` returns `False` → triggers contact-removed cleanup (deletes admin session, unlinks groups)

### LLM Failures
- `_json_call` retries once on parse failure
- Gate failure: logs warning, proceeds without filter (better to respond than miss a question)
- Synthesizer failure: falls back to `"[[TAG_ADMIN]]"`
- History extract failure: logs, continues to next chunk

### Buffer Out-of-Range Spans
- If LLM returns `start_idx < 0` or `end_idx >= n_blocks` → reject entire extract result for safety

### Periodic SCRAG Sync
- Handles partial failures in Chroma upsert/delete by reconciling hourly rather than per-operation

---

## Appendix: Key Data Flow Diagram

```
Signal Group Chat
        │ message + reaction
        ▼
signal-desktop (SQLCipher DB reader)
        │ HTTP API
        ▼
signal-bot ingest_message()
        │
        ├── raw_messages (MySQL) ◄──────────────────── history import (signal-ingest)
        │
        ├── BUFFER_UPDATE job
        │         │
        │         ├── buffers (MySQL) ← B2
        │         │
        │         ├── extract_case_from_buffer (LLM) → spans
        │         │         │
        │         │         └── make_case (LLM) → CaseResult
        │         │                   │
        │         │                   ├── B1: cases (MySQL, status=open, in_rag=0)
        │         │                   │
        │         │                   └── SCRAG: cases (MySQL, in_rag=1)
        │         │                              + ChromaDB (vector index)
        │         │
        │         └── check_case_resolved (LLM) → B1→solved→SCRAG
        │
        └── MAYBE_RESPOND job
                  │
                  ├── decide_consider (LLM gate)
                  │
                  ├── CaseSearchAgent
                  │         ├── SCRAG: ChromaDB cosine search
                  │         ├── B3: recent solved (MySQL)
                  │         └── B1: open cases (MySQL)
                  │
                  └── UltimateAgent synthesizer (Gemini)
                            └── signal.send_group_text()
```
