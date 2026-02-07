## SupportBot (Signal) — Oracle Cloud + Case-Mined RAG

This repository implements the system described in `paper.tex`:

- A **Signal group bot** (dedicated Signal number) that ingests new messages (text + images).
- A **streaming “case miner”** that turns chat into **solved support cases** (problem + solution).
- **RAG over cases (not raw chat)** using **Chroma**.
- A **two-stage reply gate** so the bot replies only when:
  - someone explicitly mentions the bot, or
  - an LLM decides the message is a real help request *and* the bot has enough evidence to answer.
- **Oracle Database** stores the relational truth (ledger, buffers, cases, jobs).
- **Google Gemini models** for all LLM tasks (via OpenAI-compatible API).
- Runs on **OCI (Always Free friendly)** as **exactly three containers**:
  - `signal-bot` (FastAPI API + Signal runtime + background worker)
  - `signal-ingest` (optional history bootstrap via linked-device flow)
  - `rag` (Chroma server)

### Models used

| Purpose | Model | Notes |
|---------|-------|-------|
| Vision/Images | `gemini-3-pro-preview` | Multimodal, best quality |
| Response generation | `gemini-3-pro-preview` | Quality output |
| History mining | `gemini-3-pro-preview` | Quality extraction |
| Gate/decision | `gemini-2.5-flash-lite` | Cheap, fast, many calls |
| Case extraction | `gemini-2.5-flash-lite` | Cheap, fast |
| Case structuring | `gemini-2.5-flash-lite` | Cheap, fast |
| Embeddings | `text-embedding-004` | Google's embedding model |

### Oracle / OCI docs quick links

- **Oracle Cloud Free Tier**: [Oracle Cloud Free Tier (Start for Free)](https://www.oracle.com/cloud/free/)
- **Always Free resource limits**: [Always Free Resources (OCI docs)](https://docs.oracle.com/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
- **Always Free Autonomous DB overview/limits**: [Always Free Autonomous Database (OCI docs)](https://docs.oracle.com/en-us/iaas/Content/Database/Concepts/adbfreeoverview.htm)
- **Autonomous DB network allowlist (ACL)**: [Configure network access control list (OCI docs)](https://docs.oracle.com/en/cloud/paas/autonomous-database/adbsa/network-access-control-list-configure.html)
- **Autonomous DB “download wallet” walkthrough**: [Launching your first free Autonomous DB instance (Oracle blog)](https://blogs.oracle.com/developers/post/launching-your-first-free-autonomous-db-instance)
- **OCI IPs / networking reference**: [Managing IP addresses (OCI docs)](https://docs.oracle.com/en-us/iaas/Content/Network/Tasks/managingIPaddresses.htm)

### Quick start (OCI / Always Free)

1. Provision OCI resources (Compute VM + Autonomous DB) using:
   - `infra/oci/README.md` (manual + Terraform paths)
2. On the VM, clone this repo and create `.env` from `env.example`.
3. Place the Autonomous DB wallet on the VM at `/var/lib/adb_wallet` (see OCI docs in `infra/oci/README.md`).
4. Start services:

```bash
sudo mkdir -p /var/lib/signal/bot /var/lib/signal/ingest /var/lib/chroma /var/lib/history /var/lib/adb_wallet
sudo docker compose up -d --build
```

If you want to bring the stack up before registering Signal accounts, set `SIGNAL_LISTENER_ENABLED=false` in `.env` and use `POST /debug/ingest` to exercise the pipeline.

### What’s implemented where

- **Signal runtime**: `signal-bot/app/signal/`
- **Oracle schema + queries**: `signal-bot/app/db/`
- **Jobs + worker**: `signal-bot/app/jobs/`
- **LLM JSON-contract calls**: `signal-bot/app/llm/`
- **RAG (Chroma)**: `signal-bot/app/rag/`
- **OCI infra**: `infra/oci/`

### Notes / constraints

- Signal bots use **unofficial tooling** (`signal-cli`). You’re responsible for complying with Signal’s terms and operational constraints.
- The MVP stores **image-derived text/observations only** (no image bytes).

### Optional history bootstrap (linked device)

- **Request token**: `POST /history/token` with:
  - `admin_id`: your Signal number in **E.164** form (used as the `signal-cli -u` username for `signal-ingest`)
  - `group_id`: the target group id
- **Get QR**: `GET /history/qr/{token}` and scan to link the `signal-ingest` device.
- `signal-ingest` will attempt to receive the initial sync stream, mine solved cases, and post them back to `signal-bot`.

