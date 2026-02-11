# SupportBot

A Signal bot for technical support groups that automatically builds a knowledge base from solved issues and answers new questions using the group's past experience.

**Documentation & Instructions:** https://supportbot.info/

---

## Overview

SupportBot monitors Signal group conversations, extracts solved support cases (problem + solution pairs) from the chat history, and uses RAG (Retrieval-Augmented Generation) to answer new questions based on accumulated knowledge.

### Key Features

- **Case-Mined RAG**: Builds knowledge from structured solved cases, not raw chat
- **Two-Stage Reply Gate**: Only responds when explicitly mentioned OR when confident it can help
- **Group Isolation**: Each group has its own independent knowledge base
- **Multi-Modal**: Processes both text and images
- **Privacy-First**: Sender phone numbers are hashed before storage
- **Bilingual**: Supports Ukrainian and English

---

## Admin Onboarding Flow

When an admin wants to connect the bot to a group:

```
┌─────────────────────────────────────────────────────────────────────┐
│  1. Admin adds bot to a Signal group (no response from bot)         │
└─────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│  2. Admin adds bot to contacts / sends direct message               │
│     → Bot sends onboarding prompt asking for group name             │
│                                                                     │
│     "Hi! I'm SupportBot. Which group would you like to connect?"    │
└─────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│  3. Admin sends group name                                          │
│     → Bot finds matching group                                      │
│     → Bot sends QR code with explanation                            │
│                                                                     │
│     "Scan this QR code in Signal to confirm access to group X..."   │
└─────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│  4. Admin scans QR code in Signal                                   │
│     → On success: Bot sends confirmation, starts processing history │
│     → On failure: Bot sends error message                           │
│                                                                     │
│     "Successfully connected to group X! Processing history..."      │
└─────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│  5. Loop back to step 2 - ask for next group                        │
│                                                                     │
│     "Want to connect another group? Send its name."                 │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          Signal Groups                               │
│                    (users add bot via phone number)                  │
└─────────────────────────────────────────────────────────────────────┘
         │                                     │
         │ Group messages                      │ Direct messages (1:1)
         ▼                                     ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         signal-bot container                         │
│  ┌─────────────────┐              ┌───────────────────────────────┐ │
│  │ Group Message   │              │ Direct Message Handler        │ │
│  │ Handler         │              │ (Admin onboarding flow)       │ │
│  │ • Ingestion     │              │ • Send onboarding prompt      │ │
│  │ • Case mining   │              │ • Find group by name          │ │
│  │ • RAG responses │              │ • Trigger QR generation       │ │
│  └─────────────────┘              │ • Send success/failure        │ │
│                                   └───────────────────────────────┘ │
│                                              │                       │
│  ┌─────────────────────────────────────────────────────────────────┐│
│  │ Background Worker                                               ││
│  │ • BUFFER_UPDATE: Extract cases from chat buffer                 ││
│  │ • MAYBE_RESPOND: Generate responses to questions                ││
│  └─────────────────────────────────────────────────────────────────┘│
│                                              │                       │
│  ┌─────────────────────────────────────────────────────────────────┐│
│  │ LLM Client (Gemini)                                             ││
│  │ • Vision (images) • Case extraction • Response generation       ││
│  └─────────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────┘
         │                                       │
         ▼                                       ▼
┌─────────────────┐                 ┌─────────────────────────────────┐
│   MySQL / Oracle│                 │      rag container (Chroma)     │
│   • raw_messages│                 │      • Vector embeddings        │
│   • buffers     │                 │      • Case retrieval           │
│   • cases       │                 │      • Per-group isolation      │
│   • admin_sess. │                 └─────────────────────────────────┘
│   • jobs        │
└─────────────────┘
         ▲
         │
┌─────────────────────────────────────────────────────────────────────┐
│                    signal-ingest container (optional)                │
│  • Generates QR codes for device linking                             │
│  • Syncs historical messages after QR scan                           │
│  • Extracts cases from history                                       │
│  • Notifies signal-bot of success/failure                            │
└─────────────────────────────────────────────────────────────────────┘
```

---

## How It Works

### 1. Message Ingestion

When a message arrives in a group:
1. Text is stored in the rolling buffer (last N messages per group)
2. Images are processed through Gemini Vision to extract text/observations
3. A `MAYBE_RESPOND` job is queued if the message might need a response
4. A `BUFFER_UPDATE` job is queued for case extraction

### 2. Case Mining (Streaming)

The background worker continuously analyzes each group's chat buffer:
1. LLM identifies solved support cases in the conversation
2. Cases are structured into: `problem_title`, `problem_summary`, `solution_summary`, `tags`
3. Cases are embedded and stored in Chroma (vector DB)
4. Each case is scoped to its source group (no cross-group leakage)

### 3. Response Generation (Two-Stage Gate)

When a potential question is detected:

**Stage 1 - Should Consider?**
- If message contains `@SupportBot` → always consider
- Otherwise, LLM decides if it's a real help request (filters spam, greetings)

**Stage 2 - Can Respond?**
- Retrieve similar cases from the group's knowledge base
- LLM decides if there's enough evidence to answer confidently
- Only responds if confident (prevents hallucination)

---

## Scaling & Multi-Group Support

### Multi-Group Isolation
- Each group has its own:
  - Message buffer (`buffers` table with `group_id`)
  - Knowledge base (Chroma queries filter by `group_id`)
  - No cross-group data leakage

### Concurrent Processing
- Job queue with `FOR UPDATE SKIP LOCKED` for parallel workers
- Stateless worker design allows horizontal scaling
- Single bot phone number can be added to many groups

### Database Options
- **MySQL** (default, containerized) — recommended for simplicity
- **Oracle** (cloud) — for OCI Always Free deployments

---

## Security & Privacy

| Feature | Implementation |
|---------|----------------|
| Sender Privacy | Phone numbers SHA256-hashed before storage |
| Group Isolation | Cases retrieved only from same group |
| History Tokens | Time-limited, single-use tokens for history bootstrap |
| Reply Gate | Two-stage LLM decision prevents spam responses |
| Input Validation | Pydantic models, database constraints |

---

## Models Used

| Purpose | Model | Notes |
|---------|-------|-------|
| Vision/Images | `gemini-3-pro-preview` | Multimodal, extracts text from images |
| Response Generation | `gemini-3-pro-preview` | Quality output for user responses |
| History Mining | `gemini-3-pro-preview` | Quality extraction from chat history |
| Gate/Decision | `gemini-2.5-flash-lite` | Fast, cheap — many calls per message |
| Case Extraction | `gemini-2.5-flash-lite` | Fast, cheap — continuous mining |
| Case Structuring | `gemini-2.5-flash-lite` | Fast, cheap |
| Embeddings | `text-embedding-004` | Google's embedding model |

---

## Project Structure

```
SupportBot/
├── signal-bot/                 # Main bot application
│   └── app/
│       ├── main.py            # FastAPI entry point, Signal listener
│       ├── config.py          # Environment configuration
│       ├── ingestion.py       # Message processing pipeline
│       ├── signal/
│       │   ├── signal_cli.py  # Signal CLI wrapper + direct messages
│       │   └── adapter.py     # Adapter interface
│       ├── db/
│       │   ├── schema_mysql.py    # MySQL DDL (includes admin_sessions)
│       │   ├── schema.py          # Oracle DDL
│       │   ├── queries_mysql.py   # MySQL queries
│       │   └── queries.py         # Oracle queries
│       ├── jobs/
│       │   ├── worker.py      # Background job processor
│       │   └── types.py       # Job type constants
│       ├── llm/
│       │   ├── client.py      # Gemini API client
│       │   ├── prompts.py     # Prompt templates (Ukrainian)
│       │   └── schemas.py     # Response schemas
│       └── rag/
│           └── chroma.py      # Chroma vector DB client
├── signal-ingest/             # History bootstrap service
│   └── ingest/
│       ├── main.py            # QR generation + history sync
│       └── config.py          # Ingest configuration
├── instructions/              # Public website (supportbot.info)
│   ├── index.html             # Bilingual instructions page
│   └── vercel.json            # Vercel deployment config
├── infra/                     # Infrastructure configs
│   └── oci/                   # Oracle Cloud setup
├── docker-compose.yml         # Container orchestration
├── env.example                # Environment template
└── paper.tex                  # Academic paper
```

---

## Quick Start

### Prerequisites
- Docker & Docker Compose
- Signal phone number for the bot
- Google Cloud API key (for Gemini)

### 1. Clone and Configure

```bash
git clone <repo>
cd SupportBot
cp env.example .env
# Edit .env with your credentials
```

### 2. Required Environment Variables

```bash
# Google AI
GOOGLE_API_KEY=your_gemini_api_key

# Signal
SIGNAL_BOT_E164=+1234567890  # Bot's phone number (E.164 format)
SIGNAL_LISTENER_ENABLED=true

# Database (MySQL default)
DB_BACKEND=mysql
MYSQL_PASSWORD=your_secure_password
```

### 3. Start Services

```bash
# Create required directories
sudo mkdir -p /var/lib/signal/bot /var/lib/signal/ingest /var/lib/chroma /var/lib/history

# Start all containers
sudo docker compose up -d --build
```

### 4. Register Signal Account

```bash
# Enter the signal-bot container
docker exec -it supportbot-signal-bot-1 bash

# Register the bot's phone number (replace with your number)
signal-cli -u +1234567890 register

# Verify with SMS code
signal-cli -u +1234567890 verify 123456
```

### 5. Add Bot to Groups

1. In Signal app: open group → tap group name → "Add members"
2. Enter the bot's phone number
3. Send the bot a direct message (1:1 chat) with the group name
4. Scan the QR code the bot sends you
5. Done — bot starts processing history and answering questions

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/healthz` | GET | Health check |
| `/history/token` | POST | Create history bootstrap token (manual API) |
| `/history/qr/{token}` | GET | Get QR code image (for web access) |
| `/history/qr-ready` | POST | Callback: QR ready, send to admin |
| `/history/link-result` | POST | Callback: Link success/failure |
| `/history/cases` | POST | Submit mined cases from history |
| `/retrieve` | POST | Query knowledge base (debug) |
| `/debug/ingest` | POST | Test message ingestion (debug) |

---

## Deployment

### Oracle Cloud (OCI Always Free)

See `infra/oci/README.md` for:
- Compute VM provisioning
- Autonomous Database setup
- Network configuration
- Wallet setup for Oracle DB

### Vercel (Instructions Site)

The documentation site at https://supportbot.info/ is deployed via Vercel:
```bash
cd instructions
npx vercel --prod
```

---

## Notes & Constraints

- Signal bots use **unofficial tooling** (`signal-cli`). Comply with Signal's terms.
- Images are processed but **not stored** — only extracted text/observations are kept.
- The bot **only responds when confident** — no hallucinated answers.
- Each group's knowledge base is **isolated** — no cross-group data sharing.
- Admin onboarding happens via **direct messages**, not in groups.

---

## License

Apache 2.0 — See [LICENSE](LICENSE)

---

## Links

- **Instructions:** https://supportbot.info/
- **Paper:** See `paper.tex` / `paper.pdf`
