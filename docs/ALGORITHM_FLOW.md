# SupportBot -- Algorithm & Architecture (Full Technical Reference)

**Last Updated**: 2026-03-10
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
9. [Answer Engine Context Layers (SCRAG / RCRAG / B3)](#9-answer-engine-context-layers-scrag--rcrag--b3)
10. [LLM Calls Reference](#10-llm-calls-reference)
11. [Worker Maintenance Tasks](#11-worker-maintenance-tasks)
12. [Case Lifecycle Summary](#12-case-lifecycle-summary)
13. [Configuration Parameters](#13-configuration-parameters)
14. [Error Handling Patterns](#14-error-handling-patterns)

---

## 1. System Overview

```
+-----------------------------------------------------------------------+
|                         SIGNAL GROUP CHAT                              |
|  Users send messages, images, emoji reactions to a Signal group       |
+-------------------------------+---------------------------------------+
                                | Messages / Reactions
                                v
+-----------------------------------------------------------------------+
|                      signal-desktop (headless)                         |
|  - Runs Signal Desktop in headless mode with SQLite DB (SQLCipher)    |
|  - Exposes HTTP API: /group/messages, /group/send, /reactions, etc.   |
|  - Polls for new messages and reactions                                |
+-------------------------------+---------------------------------------+
                                | HTTP
                                v
+-----------------------------------------------------------------------+
|                        signal-bot (FastAPI)                            |
|  +------------------+  +--------------------+  +-------------------+  |
|  | Ingest Layer     |  | Worker (job queue)  |  | HTTP API (web)    |  |
|  |                  |  |                     |  |                   |  |
|  | ingest_message   |  | BUFFER_UPDATE       |  | /case/{id}        |  |
|  | _handle_react.   |  | MAYBE_RESPOND       |  | /history/cases    |  |
|  +--------+---------+  +---------+-----------+  +-------------------+  |
|           |                      |                                     |
|           v                      v                                     |
|  +--------------------------------------------------------------------+|
|  |  MySQL Database                                                    ||
|  |  raw_messages . buffers . cases . reactions . jobs . admin_sessions||
|  +--------------------------------------------------------------------+|
|           |                      |                                     |
|           v                      v                                     |
|  +-------------------+  +------------------------+                    |
|  | ChromaDB DualRAG  |  | Gemini API (LLM)       |                    |
|  | cases_scrag       |  | gemini-2.5-pro (sub)    |                    |
|  | cases_rcrag       |  | gemini-2.5-flash (gate) |                    |
|  | (solved + recom.) |  | text-embedding-004      |                    |
|  +-------------------+  +------------------------+                    |
|                                                                        |
|  +-------------------+                                                |
|  | Cloudflare R2     |                                                |
|  | Image storage     |                                                |
|  +-------------------+                                                |
+-----------------------------------------------------------------------+
         ^
         |  HTTP POST /history/cases
+--------+--------------------------------------------------------------+
|                       signal-ingest                                    |
|  History ingestion service:                                            |
|  - Triggers QR-code linking of admin's Signal account                 |
|  - Reads 45-day chat history from signal-desktop                      |
|  - Extracts solved cases with LLM                                     |
|  - Posts cases to signal-bot                                           |
+-----------------------------------------------------------------------+
         ^
         |  Browser / signal-web
+--------+--------------------------------------------------------------+
|                      signal-web (Next.js)                              |
|  Public web UI for viewing case details, chat history                  |
+-----------------------------------------------------------------------+
```

---

## 2. Services & Components

| Service | Technology | Role |
|---------|-----------|------|
| `signal-desktop` | Python FastAPI + SQLCipher | Reads/writes Signal Desktop SQLite DB; HTTP API for messages and reactions |
| `signal-bot` | Python FastAPI | Core backend: ingest, worker queues, LLM orchestration, case DB, dual-RAG |
| `signal-ingest` | Python | History import: QR-link admin account, bulk-extract cases from past messages |
| `signal-web` | Next.js (React) | Case viewer web app; displays case details, chat history |
| MySQL | MySQL 8 | Primary persistent store: messages, buffers, cases, jobs, sessions |
| ChromaDB | Chroma | Two-collection vector store: SCRAG (solved) + RCRAG (recommendation) |
| Cloudflare R2 | S3-compatible | Image blob storage (with local fallback) |
| Gemini API | Google AI Studio | All LLM calls: gating, extraction, embedding, synthesis |

### Key source files

```
signal-bot/app/
+-- main.py                  -- FastAPI app, signal listener, reaction handler
+-- ingestion.py             -- ingest_message(): store + enqueue jobs
+-- r2.py                    -- Cloudflare R2 image storage
+-- jobs/worker.py           -- BUFFER_UPDATE and MAYBE_RESPOND job handlers
+-- agent/
|   +-- ultimate_agent.py    -- UltimateAgent: gate -> parallel agents -> synthesize
|   +-- case_search_agent.py -- CaseSearchAgent: SCRAG + RCRAG + B3 + RCRAG-DB
|   +-- docs_agent.py        -- DocsAgent: answers from Google Docs
+-- llm/
|   +-- client.py            -- LLMClient: all Gemini API calls, model cascades
|   +-- prompts.py           -- All system prompts (P_BLOCKS_SYSTEM, etc.)
|   +-- schemas.py           -- Pydantic output schemas
+-- db/
|   +-- queries_mysql.py     -- All SQL queries
|   +-- schema_mysql.py      -- DB schema (create tables + migrations)
+-- rag/chroma.py            -- DualRag (SCRAG + RCRAG), ChromaRag wrapper

signal-ingest/ingest/main.py -- History ingestion pipeline
signal-desktop/app/
+-- db_reader.py             -- Reads Signal Desktop SQLite (SQLCipher)
+-- main.py                  -- FastAPI HTTP API over db_reader
```

---

## 3. Data Stores

### MySQL Tables

| Table | Purpose |
|-------|---------|
| `raw_messages` | Every ingested message: `message_id`, `group_id`, `ts`, `sender_hash`, `sender_name`, `content_text` (with OCR'd image JSON), `image_paths_json`, `reply_to_id` |
| `buffers` | Per-group rolling message buffer (plain text, used for LLM case extraction) |
| `cases` | All cases: `case_id`, `group_id`, `status` (solved/recommendation/archived), `problem_title`, `problem_summary`, `solution_summary`, `tags_json`, `evidence_image_paths_json`, `in_rag`, `closed_emoji`, `embedding_json` |
| `case_evidence` | Links cases to their evidence messages (`case_id`, `message_id`) |
| `reactions` | Emoji reactions: `group_id`, `target_ts`, `target_author`, `sender_hash`, `emoji` |
| `jobs` | Worker job queue: `job_id`, `type`, `payload_json`, `status`, `attempts`, `run_after` |
| `admin_sessions` | Admin session state: `admin_id`, `state`, `pending_group_id`, `lang` |
| `history_tokens` | One-time tokens for history import authorization |
| `chat_groups` | Group metadata: `group_id`, `group_name`, `docs_urls`, `union_id`, `tag_targets_json`, `ingesting` |
| `admins_groups` | Admin-to-group links |

### ChromaDB (Dual-RAG)

Two separate collections managed by `DualRag`:

**SCRAG** (`cases_scrag`): Solved cases with confirmed solutions.
- **document**: `[SOLVED] <title>\nProblem: ...\nSolution: ...\ntags: ...`
- **embedding**: vector from `text-embedding-004`
- **metadata**: `{group_id, status}`

**RCRAG** (`cases_rcrag`): Recommendation cases with unconfirmed advice.
- Same format but with `[RECOMMENDATION]` prefix
- When a recommendation is confirmed, it is moved from RCRAG to SCRAG

---

## 4. Live Message Pipeline

Every message from Signal Desktop flows through this path:

```
Signal Desktop polls its SQLite DB every few seconds
        |
        v
SignalDesktopAdapter.listen_forever()
  - Gets new group messages -> _handle_group_message(m)
  - Gets reactions          -> _handle_reaction(r)
  - Gets contact-removed    -> _handle_contact_removed(phone)
        |
        v (group message)
ingest_message(settings, db, llm, message_id, group_id, sender, ts, text, image_paths)
        |
        +- Image processing (if attachments):
        |     for each image:
        |       llm.image_to_text_json(image_bytes, context_text)
        |         -> ImgExtract {observations: List[str], extracted_text: str}
        |       Upload to R2 (Cloudflare) or local fallback
        |       Append to content_text: "\n\n[image]\n{json}"
        |
        +- insert_raw_message(db, RawMessage{...})
        |     (idempotent; skips if message_id already exists)
        |
        +- enqueue_job(db, BUFFER_UPDATE, payload)
           enqueue_job(db, MAYBE_RESPOND, payload)
```

### Double Response Prevention

The worker tracks responded messages via `_responded_messages` (a dedup dict keyed by message_id). If a timed-out job is retried, the worker skips sending a duplicate response.

---

## 5. Case Extraction Pipeline (BUFFER_UPDATE)

Triggered for every new message. Purpose: maintain the rolling buffer (B2) and extract/promote cases.

```
BUFFER_UPDATE job consumed by worker_loop_forever()
        |
        v
_handle_buffer_update(deps, payload)
        |
        +- Load message from raw_messages
        +- Check positive reactions on this message
        +- Mark as [BOT] if sender_hash == bot_sender_hash
        +- Append formatted buffer line:
        |     "{sender_hash}[BOT?] ts={ts} msg_id={msg_id} reactions=N\n{content_text}\n\n"
        |
        +- Trim buffer:
        |     - Remove messages older than buffer_max_age_hours (168h = 7 days)
        |     - Remove oldest messages if > buffer_max_messages (300)
        |
        +- Parse buffer into indexed message blocks (BufferMessageBlock)
        +- Filter out [BOT] blocks for extraction input
        |
        +--- UNIFIED BUFFER ANALYSIS (single LLM call) ---
        |
        |   llm.unified_buffer_analysis(buffer, existing_recommendation_cases)
        |     -> UnifiedBufferResult {
        |          new_cases: [{start_idx, end_idx, status, problem_title, ...}]
        |          promotions: [{case_id, solution_summary}]
        |          updates: [{case_id, solution_summary, additional_evidence_ids}]
        |        }
        |
        |   For each new case:
        |     Semantic dedup: embed -> find_similar_case()
        |       if similar -> merge_case()
        |       else       -> insert_case()
        |
        |     if status == "solved":
        |       rag.upsert_case() into SCRAG, mark_case_in_rag()
        |       accepted_ranges.append(span) -> remove from buffer
        |     else (recommendation):
        |       rag.upsert_case() into RCRAG
        |       keep messages in buffer
        |
        |   For each promotion (recommendation -> solved):
        |     update_case_to_solved(), upsert to SCRAG
        |     (DualRag automatically removes from RCRAG when upserting to SCRAG)
        |
        +--- Update buffer ---
            Remove message spans that became solved cases
            set_buffer(db, group_id, buffer_new)
```

### Buffer Line Format

```
{sender_hash}[BOT] ts={timestamp_ms} msg_id={uuid} reply_to={uuid} reactions=N
{content_text}

```

- `[BOT]` tag: only for messages from the bot's own phone number
- `reactions=N`: count of positive emoji reactions
- `reply_to=`: quoted message ID
- `msg_id=`: used by LLM to output `evidence_ids` for case linking

---

## 6. Answer Pipeline (MAYBE_RESPOND)

Triggered for every new message. Purpose: decide if and how to respond.

```
MAYBE_RESPOND job consumed by worker_loop_forever()
        |
        v
_handle_maybe_respond(deps, payload)
        |
        +- Load message from raw_messages
        +- Skip if content_text is empty (system notification)
        +- Double response check: skip if message_id in _responded_messages
        |
        +- Check group has active linked admins
        |
        +--- GATE: decide_consider() ---
        |
        |   context_text = last N messages (excluding current)
        |   gate_images  = first 3 attached images (if present)
        |
        |   gate = llm.decide_consider(message, context, images)
        |     [P_DECISION_SYSTEM prompt + GATE_CASCADE: gemini-2.5-flash -> gemini-2.0-flash]
        |     -> DecisionResult {consider: bool, tag: str}
        |
        |   Tags: new_question | ongoing_discussion | statement | noise
        |
        |   if not gate.consider AND not force (bot mention):
        |     STOP (silent)
        |
        +--- ULTIMATE AGENT (parallel search + synthesis) ---
        |
        |   answer = UltimateAgent.answer(question, group_id, db, lang, context, images)
        |
        |   +-- CaseSearchAgent.answer() --------- (parallel) ----------+
        |   |  1. Embed query with text-embedding-004                    |
        |   |  2. SCRAG: cosine search cases_scrag (top 3)              |
        |   |  3. RCRAG: cosine search cases_rcrag (top 3)              |
        |   |  4. B3: get_recent_solved_cases(db, group_id, since_ts)   |
        |   |  5. RCRAG-DB: recommendation cases not yet in RAG         |
        |   |  Distance threshold: 0.75 (cosine)                        |
        |   |                                                            |
        |   |  Priority:                                                 |
        |   |    SCRAG or B3 results -> direct answer with case link     |
        |   |    RCRAG results -> answer with "not confirmed" caveat     |
        |   |    Nothing -> "No relevant cases found."                   |
        |   +------------------------------------------------------------+
        |
        |   +-- DocsAgent.answer() --------------- (parallel) ----------+
        |   |  1. Fetch Google Docs from chat_groups.docs_urls           |
        |   |  2. Pass docs + question to Gemini                         |
        |   |  3. Return answer or INSUFFICIENT_INFO                     |
        |   +------------------------------------------------------------+
        |
        |   Synthesizer (SUBAGENT_CASCADE: gemini-2.5-pro -> ... -> gemini-2.5-flash):
        |     Receives case_agent output + docs_agent output
        |     Generates final user-facing answer
        |     If no relevant info found -> "[[TAG_ADMIN]]"
        |
        +--- SEND ---
        |
        |   [[TAG_ADMIN]] -> replace with @mention of:
        |     1. Per-group tag targets (chat_groups.tag_targets_json), or
        |     2. Active admins for this group
        |
        |   signal.send_group_text(
        |       group_id, text, quote_timestamp, quote_author, ...)
        |   Track in _responded_messages to prevent duplicates
        |
        +---
```

### Gate Tags

| Tag | Meaning | consider |
|-----|---------|----------|
| `new_question` | New support question | **true** |
| `ongoing_discussion` | Continues an active thread in context | **true** |
| `statement` | Summary / conclusion / "I solved it" without asking | **false** |
| `noise` | Greeting, "ok", emoji-only, off-topic | **false** |

Key rules:
- `consider=true` for technical problem descriptions (even if phrased as statements)
- `consider=false` for summaries starting with "Pidsumoviuiuchy", "Reziuumiuiuchy" etc.
- Bot mention (`force=true`) bypasses the gate

---

## 7. Emoji Reaction & Case Confirmation

```
Signal Desktop receives emoji reaction
        |
        v
_handle_reaction(r: InboundReaction)
        |
        +- if r.is_remove:
        |     delete_reaction(db, ...)
        |
        +- else:
              upsert_reaction(db, ...)
              if r.emoji in POSITIVE_EMOJI:
                n = confirm_cases_by_evidence_ts(db, group_id, target_ts, emoji)
                if n > 0: log case confirmation
```

`POSITIVE_EMOJI` includes thumbs up, heart, checkmark, and other approval emoji.

`confirm_cases_by_evidence_ts()`: finds cases whose evidence messages contain the reacted-to timestamp, updates `status='solved'` and sets `closed_emoji`.

---

## 8. History Ingestion (signal-ingest)

```
Admin initiates history import via DM with bot
        |
        v
signal-bot: creates HISTORY_LINK job
  -> POST signal-ingest/jobs

signal-ingest job flow:
        |
        +- 1. Set ingesting=1 flag on group (concurrent ingestion guard)
        +- 2. Reset Signal Desktop (clear previous account)
        +- 3. Request new QR code from signal-desktop
        +- 4. Send QR image to admin via signal-bot DM
        +- 5. Wait for admin to scan QR (timeout: 5 min)
        +- 6. Fetch historical messages from signal-desktop
        +- 7. Chunk messages and extract cases with LLM (P_BLOCKS_SYSTEM)
        +- 8. POST extracted cases to signal-bot /history/cases
        |       signal-bot processes: make_case() -> dedup -> insert -> SCRAG/RCRAG
        +- 9. Reset Signal Desktop (unlink admin account for privacy)
        +- 10. Clear ingesting flag
```

### Ingestion Guard

The `chat_groups.ingesting` flag prevents the live worker from processing BUFFER_UPDATE jobs for a group during history import. Stale flags are cleaned up on startup (5-minute timeout safety).

---

## 9. Answer Engine Context Layers (SCRAG / RCRAG / B3)

```
+---------------------------------------------------------------+
| SCRAG -- Solved Cases RAG (ChromaDB collection: cases_scrag)   |
| +- Source: solved cases with confirmed solution                |
| +- Search: cosine similarity (text-embedding-004)              |
| +- Filter: by group_id (union-aware: searches all union groups)|
| +- Top-K: 3 results, distance threshold: 0.75                 |
+---------------------------------------------------------------+

+---------------------------------------------------------------+
| RCRAG -- Recommendation RAG (ChromaDB collection: cases_rcrag) |
| +- Source: recommendation cases (unconfirmed advice)           |
| +- Same search mechanics as SCRAG                              |
| +- Lower trust: synthesizer adds "not confirmed" caveat        |
+---------------------------------------------------------------+

+---------------------------------------------------------------+
| B3 -- Recently Solved Buffer (MySQL query)                     |
| +- Source: solved cases whose evidence_ts falls in B2 window   |
| +- Purpose: catches freshly solved cases                       |
+---------------------------------------------------------------+

+---------------------------------------------------------------+
| B2 -- Rolling Message Buffer (MySQL buffers table)             |
| +- Content: all recent group messages as formatted text        |
| +- Age limit: buffer_max_age_hours (168h = 7 days)            |
| +- Size limit: buffer_max_messages (300)                       |
| +- Use: case extraction input (BUFFER_UPDATE)                  |
+---------------------------------------------------------------+
```

### Response Decision Tree

```
CaseSearchAgent.answer(question, group_id, db)
        |
        +- Embed query -> search SCRAG + RCRAG (both independently)
        +- Also query B3 (recent solved)
        |
        +- SCRAG or B3 results?
        |     YES -> format: problem + solution + case link
        |            -> UltimateAgent synthesizer generates direct answer
        |
        +- RCRAG results?
        |     YES -> format with "recommendation, not confirmed" caveat
        |            -> synthesizer generates qualified answer
        |
        +- Nothing -> "No relevant cases found."
              -> UltimateAgent returns "[[TAG_ADMIN]]"
              -> Worker replaces with @mention
```

---

## 10. LLM Calls Reference

All calls use Gemini API via OpenAI-compatible endpoint.

### Model Cascades

- **SUBAGENT_CASCADE**: `gemini-2.5-pro` -> `gemini-3.1-pro-preview` -> `gemini-2.5-flash`
  Used for: synthesizer, subagent calls
- **GATE_CASCADE**: `gemini-2.5-flash` -> `gemini-2.0-flash`
  Used for: gate (decide_consider)

### Per-call defaults (from config.py)

| Setting | Default Model |
|---------|--------------|
| `model_img` | gemini-3.1-pro-preview |
| `model_decision` | gemini-2.5-flash |
| `model_extract` | gemini-3.1-pro-preview |
| `model_case` | gemini-3.1-pro-preview |
| `model_respond` | gemini-3.1-pro-preview |
| `model_blocks` | gemini-3.1-pro-preview |
| `embedding_model` | text-embedding-004 |

### Call Summary

| Call | Function | Purpose | Output Schema |
|------|----------|---------|---------------|
| Image OCR | `llm.image_to_text_json()` | Extract text & observations from image | `ImgExtract` |
| Gate | `llm.decide_consider()` | Filter noise / classify message | `DecisionResult` |
| Unified buffer | `llm.unified_buffer_analysis()` | Extract new cases + promote recommendations in one call | `UnifiedBufferResult` |
| Case structure | `llm.make_case()` | Structure a case block into fields | `CaseResult` |
| Embed | `llm.embed()` | Vector for dedup + RAG search | `List[float]` |
| Synthesize | subagent cascade | Final user-facing answer | Free text |
| History extract | P_BLOCKS_SYSTEM | Extract solved cases from history chunk | `BlocksResult` |

### Embedding & Deduplication

Every case is embedded twice:
1. **Dedup embed**: `"{problem_title}\n{problem_summary}"` -- used by `find_similar_case()` to prevent duplicates
2. **RAG embed**: full doc_text (`[SOLVED/RECOMMENDATION] title\nProblem: ...\nSolution: ...\ntags: ...`) -- used for semantic search

---

## 11. Worker Maintenance Tasks

### SCRAG Sync (hourly)
```python
_run_sync_rag(deps)
```
Compares ChromaDB entries against MySQL active case IDs. Removes stale ChromaDB entries whose MySQL case no longer exists.

### Ingesting Flag Cleanup (startup)
Stale `ingesting=1` flags older than 5 minutes are cleared on worker startup, preventing stuck groups.

### Job Timeout Safety
Each job has a hard timeout of 180 seconds (`_JOB_TIMEOUT_SECONDS`). If a job exceeds this, the main loop abandons it and marks it failed.

---

## 12. Case Lifecycle Summary

```
MESSAGE ARRIVES
       |
       v
raw_messages: inserted (idempotent)
       |
       +-- BUFFER_UPDATE: added to B2 buffer
       |         |
       |         +-- Unified LLM analysis:
       |         |     |
       |         |     +-- new_case(recommendation) -> RCRAG + keep in B2
       |         |     +-- new_case(solved) -> SCRAG + remove from B2
       |         |     +-- promotion(recommendation -> solved) -> move RCRAG to SCRAG
       |         |     +-- update(existing case with new evidence)
       |
       +-- MAYBE_RESPOND: gate -> UltimateAgent -> send
                            |
                            +-- CaseSearchAgent (SCRAG + RCRAG + B3)
                            +-- DocsAgent (Google Docs)
                            +-- Synthesizer -> signal.send_group_text()

EMOJI REACTION
       |
       +-- upsert_reaction -> confirm_cases_by_evidence_ts()
                 -> UPDATE cases SET status=solved, closed_emoji=emoji

HISTORY IMPORT
       |
       +-- signal-ingest: LLM extracts from history chunks
                 -> POST /history/cases
                 -> make_case() -> insert/merge -> SCRAG/RCRAG
```

---

## 13. Configuration Parameters

Key settings from `Settings` (loaded from environment / `.env`):

| Setting | Default | Description |
|---------|---------|-------------|
| `buffer_max_age_hours` | 168 (7 days) | B2 buffer: drop messages older than N hours |
| `buffer_max_messages` | 300 | B2 buffer: maximum message count |
| `worker_poll_seconds` | 1 | Job queue poll interval |
| `signal_bot_e164` | (required) | Bot's own phone number |
| `signal_bot_storage` | `/var/lib/signal/bot` | Path to Signal storage |
| `signal_desktop_url` | `http://signal-desktop-arm64:8001` | signal-desktop HTTP API base URL |
| `use_signal_desktop` | false | Use Signal Desktop adapter vs signal-cli |
| `public_url` | `https://supportbot.info` | Base URL for case links |
| `chroma_url` | `http://rag:8000` | ChromaDB server URL |
| `chroma_collection` | `cases` | Base name for collections (-> `cases_scrag`, `cases_rcrag`) |
| `context_last_n` | 40 | Number of recent messages for gate context |
| `max_image_size_bytes` | 5,000,000 | Skip images larger than this |
| `admin_session_stale_minutes` | 30 | Session timeout for re-welcome |

---

## 14. Error Handling Patterns

### Idempotency
- `insert_raw_message`: skips duplicate `message_id` (INSERT IGNORE)
- `rag.upsert_case`: Chroma upsert replaces existing entry
- `_responded_messages`: prevents duplicate bot responses on job retries

### Worker Retries
- Failed jobs are retried up to 3 times (`fail_job` increments `attempts`)
- After 3 failures, job is permanently marked failed
- Per-job hard timeout: 180 seconds

### Model Cascades
- SUBAGENT_CASCADE: if first model fails, falls back to next in chain
- GATE_CASCADE: same pattern for gate calls

### Signal Adapter Fallbacks
- Signal Desktop not available at boot -> listener started lazily
- `send_direct_text` returns `False` -> triggers contact-removed cleanup

### LLM Failures
- `_json_call` retries once on parse failure
- Gate failure: logs warning, proceeds without filter
- Synthesizer failure: falls back to `"[[TAG_ADMIN]]"`

### Concurrent Ingestion Guard
- `chat_groups.ingesting` flag prevents worker from processing group during history import
- Stale flags cleaned up on startup (5-minute timeout)

### R2 Upload
- Infinite retry with exponential backoff -- uploads never silently fail

---

## Appendix: Key Data Flow Diagram

```
Signal Group Chat
        | message + reaction
        v
signal-desktop (SQLCipher DB reader)
        | HTTP API
        v
signal-bot ingest_message()
        |
        +-- raw_messages (MySQL) <--- history import (signal-ingest)
        |
        +-- BUFFER_UPDATE job
        |         |
        |         +-- buffers (MySQL) -- B2
        |         |
        |         +-- unified_buffer_analysis (LLM) -> new cases + promotions
        |         |         |
        |         |         +-- recommendation: RCRAG (ChromaDB cases_rcrag)
        |         |         +-- solved: SCRAG (ChromaDB cases_scrag)
        |         |         +-- promotion: RCRAG -> SCRAG
        |
        +-- MAYBE_RESPOND job
                  |
                  +-- decide_consider (LLM gate, GATE_CASCADE)
                  |
                  +-- UltimateAgent (parallel):
                  |     +-- CaseSearchAgent: SCRAG + RCRAG + B3
                  |     +-- DocsAgent: Google Docs
                  |
                  +-- Synthesizer (SUBAGENT_CASCADE)
                            +-- signal.send_group_text()
```
