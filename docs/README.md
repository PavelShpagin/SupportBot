# SupportBot Documentation Index

**Last Updated**: 2026-03-10
**Status**: Production

---

## Quick Links

### Core Documentation
1. **[ALGORITHM_FLOW.md](./ALGORITHM_FLOW.md)** - Complete technical flow: dual-RAG, UltimateAgent, case extraction
2. **[CASE_EXAMPLES.md](./CASE_EXAMPLES.md)** - Real evaluation examples with judge outputs
3. **[FINAL_EVALUATION_REPORT.md](./FINAL_EVALUATION_REPORT.md)** - Production readiness report (historical, 2026-02-11)
4. **[DEPLOYMENT.md](./DEPLOYMENT.md)** - Oracle Cloud deployment guide
5. **[SIGNAL_REGISTRATION.md](./SIGNAL_REGISTRATION.md)** - Signal Desktop linking, QR flow, persistence
6. **[SESSION_MANAGEMENT.md](./SESSION_MANAGEMENT.md)** - Admin session lifecycle, commands
7. **[MEDIA_HANDLING.md](./MEDIA_HANDLING.md)** - Image processing, R2 storage, OCR pipeline

---

## Architecture Overview

```
Signal Group Chat
        | messages + reactions
        v
signal-desktop (headless, SQLCipher DB reader, HTTP API)
        | HTTP
        v
signal-bot (FastAPI)
  +-- Ingest Layer (ingest_message, reaction handler)
  +-- Worker (BUFFER_UPDATE, MAYBE_RESPOND jobs)
  +-- UltimateAgent (CaseSearchAgent + DocsAgent in parallel, synthesizer)
  +-- HTTP API (case viewer, history import)
        |
        +--- MySQL 8 (messages, buffers, cases, jobs, sessions)
        +--- ChromaDB dual-RAG (SCRAG: solved, RCRAG: recommendation)
        +--- Gemini API (LLM: gating, extraction, synthesis, embedding)
        +--- Cloudflare R2 (image storage)
        |
signal-ingest (history import via QR linking)
signal-web (Next.js case viewer)
```

### Services (Docker Compose)

| Service | Port | Description |
|---------|------|-------------|
| `signal-bot` | 8000 | Main FastAPI backend: ingest, worker, LLM orchestration, RAG |
| `signal-desktop` | 8001 | Headless Signal Desktop, SQLCipher reader, HTTP API |
| `signal-ingest` | - | History import: QR-link admin account, bulk-extract cases |
| `signal-web` | - | Next.js case viewer UI |
| `db` | 3306 | MySQL 8 |
| `rag` | 8002 | ChromaDB vector database |

No Redis. No Oracle DB (legacy only). No signal-cli-rest-api.

---

## Case Pipeline: Dual-RAG (SCRAG + RCRAG)

### Two-RAG Architecture

The bot uses two separate ChromaDB collections managed by `DualRag` in `signal-bot/app/rag/chroma.py`:

```
SCRAG  -- Solved Cases RAG        (ChromaDB collection: cases_scrag)
           Confirmed solutions, highest trust, permanent

RCRAG  -- Recommendation Cases RAG (ChromaDB collection: cases_rcrag)
           Unconfirmed advice/recommendations, lower trust

B2     -- Rolling Message Buffer   (MySQL buffers table, 300 msgs / 7 days)
B3     -- Recent Solved Buffer     (solved cases with evidence still in B2 window)
```

### Case Statuses

Only three statuses exist -- there is NO "open" status:

| Status | Meaning | Stored in |
|--------|---------|-----------|
| `solved` | Confirmed solution | SCRAG (ChromaDB) + MySQL |
| `recommendation` | Unconfirmed advice | RCRAG (ChromaDB) + MySQL |
| `archived` | Superseded / merged | MySQL only |

### Data Flow

```
Incoming message (live or ingest)
        |
        v
  raw_messages table
        |
        v
  B2: Rolling buffer (buffers table, 300 msgs / 7 days)
        |
        v
  Unified LLM buffer analysis (single multimodal call)
        |
        +-- New case: recommendation (problem, no confirmed solution)
        |       --> RCRAG: embed + upsert
        |       --> Keep messages in B2
        |
        +-- New case: solved (problem + confirmed solution)
        |       --> SCRAG: embed + upsert
        |       --> Remove consumed spans from B2
        |
        +-- Promotion: existing recommendation --> solved
                --> Move from RCRAG to SCRAG
```

### Answer Engine Context

When answering a user question, `CaseSearchAgent` queries four layers:

| Layer | Source | Description |
|-------|--------|-------------|
| SCRAG | ChromaDB cosine search (`cases_scrag`) | All-time solved cases, filtered by group |
| RCRAG | ChromaDB cosine search (`cases_rcrag`) | Recommendation cases, unconfirmed |
| B3 | `get_recent_solved_cases()` | Solved cases still within the B2 window |
| RCRAG-DB | MySQL query | Recommendation cases not yet in RAG |

**Response rules:**
- SCRAG or B3 hit with solution --> synthesize direct answer + case link
- RCRAG hit --> synthesize answer with caveat ("not confirmed") + case link
- Nothing found --> `[[TAG_ADMIN]]` (replaced with @mention of admin)

---

## LLM Configuration

All LLM calls use Gemini models via Google AI Studio OpenAI-compatible endpoint.

| Purpose | Model (default) |
|---------|----------------|
| Subagent cascade | gemini-2.5-pro --> gemini-3.1-pro-preview --> gemini-2.5-flash |
| Gate cascade | gemini-2.5-flash --> gemini-2.0-flash |
| Image OCR (`model_img`) | gemini-3.1-pro-preview |
| Case extraction (`model_extract`) | gemini-3.1-pro-preview |
| Case structuring (`model_case`) | gemini-3.1-pro-preview |
| History blocks (`model_blocks`) | gemini-3.1-pro-preview |
| Gate (`model_decision`) | gemini-2.5-flash |
| Embedding | text-embedding-004 |

---

## Key Source Files

```
signal-bot/app/
+-- main.py                    -- FastAPI app, signal listener, reaction handler
+-- ingestion.py               -- ingest_message(): store + enqueue jobs
+-- r2.py                      -- Cloudflare R2 image storage
+-- jobs/worker.py             -- BUFFER_UPDATE and MAYBE_RESPOND job handlers
+-- agent/
|   +-- ultimate_agent.py      -- UltimateAgent: gate -> parallel agents -> synthesize
|   +-- case_search_agent.py   -- CaseSearchAgent: SCRAG + RCRAG + B3 retrieval
|   +-- docs_agent.py          -- DocsAgent: answers from Google Docs
+-- llm/
|   +-- client.py              -- LLMClient: all Gemini API calls, cascades
|   +-- prompts.py             -- All system prompts (Ukrainian)
|   +-- schemas.py             -- Pydantic output schemas
+-- db/
|   +-- queries_mysql.py       -- All SQL queries
|   +-- schema_mysql.py        -- DB schema (DDL + migrations)
+-- rag/chroma.py              -- DualRag (SCRAG + RCRAG), ChromaRag wrapper
+-- config.py                  -- Settings with env var defaults

signal-ingest/ingest/main.py   -- History ingestion pipeline
signal-desktop/app/
+-- db_reader.py               -- Reads Signal Desktop SQLite (SQLCipher)
+-- main.py                    -- FastAPI HTTP API over db_reader

signal-web/                    -- Next.js case viewer
```

---

## Commands (Admin DM)

| Command | Description |
|---------|-------------|
| `/en` | Switch to English UI |
| `/ua` | Switch to Ukrainian UI |
| `/wipe` | Erase all groups/cases/sessions for this admin |
| `/union <group>` | Join two groups into a union (shared RAG + docs) |
| `/split` | Remove current group from its union |
| `/tag <phone1>,<phone2>` | Set per-group mention targets for escalation |

---

## Quick Reference

### Evaluation Data

```
tests/
+-- fixtures/images/           -- Test image fixtures
```

### Key Commands

```bash
# Run tests
pytest tests/ -v

# Deploy (full)
./scripts/deploy-oci.sh full

# Deploy (push code + restart)
./scripts/deploy-oci.sh push

# SSH into VM
./scripts/deploy-oci.sh ssh
```

---

**Document Version**: 2.0
**Last Updated**: 2026-03-10
