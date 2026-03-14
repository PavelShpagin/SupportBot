# SupportBot Deployment Guide

Complete instructions for deploying SupportBot to Oracle Cloud Infrastructure (OCI).

## Quick Start (Existing VM)

If you already have an OCI VM at `161.33.64.115`:

```bash
# 1. Make sure .env has correct values
#    ORACLE_VM_IP=161.33.64.115
#    ORACLE_VM_KEY=~/.ssh/supportbot_ed25519

# 2. Deploy
./scripts/deploy-oci.sh full

# 3. Link Signal account (scan QR with your phone)
./scripts/deploy-oci.sh link-signal

# 4. Set bot avatar
./scripts/deploy-oci.sh set-avatar
```

---

## Architecture Overview

```
+------------------+     +-------------------+     +------------------+
|  Signal Users    | --> |   OCI VM          | --> |  Google AI API   |
|  (Groups/DMs)    |     |   161.33.64.115   |     |  (Gemini + GPT)  |
+------------------+     +-------------------+     +------------------+
                               |
                    +----------+----------+----------+
                    |          |          |          |
              +-----v----+ +---v---+ +----v----+ +---v---------+
              | signal-  | | MySQL | | Chroma  | | signal-     |
              | bot:8000 | | :3306 | | :8002   | | desktop:8001|
              +----------+ +-------+ +---------+ +-------------+
                    |
              +-----v------+     +----------+
              | signal-    |     | signal-  |
              | ingest     |     | web      |
              +------------+     +----------+
```

### Services (Docker Compose)

| Service | Port | Description |
|---------|------|-------------|
| `signal-bot` | 8000 | Main FastAPI backend + worker |
| `signal-desktop` | 8001 | Headless Signal Desktop (SQLCipher DB) |
| `signal-ingest` | - | History import via QR linking |
| `signal-web` | - | Next.js case viewer |
| `db` | 3306 | MySQL 8 |
| `rag` | 8002 | ChromaDB (dual-RAG: cases_scrag + cases_rcrag) |

**No Redis.** **No signal-cli-rest-api.**

---

## Prerequisites

### SSH Key

```bash
ssh-keygen -t ed25519 -f ~/.ssh/supportbot_ed25519 -N ""
```

### Signal Bot Number

You need a phone number for the bot (e.g., `+380730017651`).

---

## Deployment Steps

### Step 1: Configure Environment

Edit `.env` with your values:

```bash
# Required
GOOGLE_API_KEY=AIzaSyB...        # Your Gemini API key
SIGNAL_BOT_E164=+380730017651    # Bot phone number

# OCI VM
ORACLE_VM_IP=161.33.64.115
ORACLE_VM_KEY=~/.ssh/supportbot_ed25519

# Signal Desktop
USE_SIGNAL_DESKTOP=true
SIGNAL_DESKTOP_URL=http://signal-desktop-arm64:8001
```

### Step 2: Initialize VM (First Time Only)

```bash
./scripts/deploy-oci.sh init
```

### Step 3: Deploy Application

```bash
./scripts/deploy-oci.sh full
```

This will:
1. Sync project files to VM
2. Build Docker images
3. Start all services (docker-compose.yml)
4. Run health checks

### Step 4: Link Signal Account

```bash
./scripts/deploy-oci.sh link-signal
```

Or manually:
1. `curl http://161.33.64.115:8001/screenshot > qr.png`
2. Signal -> Settings -> Linked Devices -> Link New Device
3. Choose "Transfer message history" when prompted

### Step 5: Set Bot Avatar

```bash
./scripts/deploy-oci.sh set-avatar
```

---

## Testing the Bot

### Health Check

```bash
curl http://161.33.64.115:8000/healthz
# Expected: {"ok":true}
```

### Bootstrap Group History

DM the bot from your phone:
1. Send any message -> welcome + language detection
2. Send the group name -> bot finds group, generates QR
3. Scan QR -> history synced, cases extracted

---

## Monitoring

```bash
# All logs
./scripts/deploy-oci.sh logs

# Specific service
./scripts/deploy-oci.sh logs signal-bot

# SSH into VM
./scripts/deploy-oci.sh ssh
```

---

## Troubleshooting

### Bot not responding?

1. Check logs: `./scripts/deploy-oci.sh logs signal-bot`
2. Verify Signal is linked: `curl http://161.33.64.115:8001/status`
3. Check API health: `curl http://161.33.64.115:8000/healthz`

### ChromaDB errors?

```bash
./scripts/deploy-oci.sh ssh
cd supportbot
docker compose restart rag
```

### Database issues?

```bash
docker compose exec db mysql -u supportbot -psupportbot supportbot
```

---

## Commands Reference

| Command | Description |
|---------|-------------|
| `./scripts/deploy-oci.sh init` | First-time VM setup |
| `./scripts/deploy-oci.sh push` | Push code + restart |
| `./scripts/deploy-oci.sh full` | Push + build + start |
| `./scripts/deploy-oci.sh ssh` | SSH into VM |
| `./scripts/deploy-oci.sh logs [service]` | View logs |
| `./scripts/deploy-oci.sh status` | Service status |
| `./scripts/deploy-oci.sh stop` | Stop services |
| `./scripts/deploy-oci.sh restart` | Restart services |
| `./scripts/deploy-oci.sh link-signal` | Link Signal account |
| `./scripts/deploy-oci.sh set-avatar` | Set bot avatar |

---

## supportbot.info Website

The landing page is deployed on Vercel:
```bash
cd instructions
vercel --prod
```

---

## Costs

| Resource | Cost |
|----------|------|
| OCI VM (Always Free A1.Flex) | Free |
| Google AI (Gemini API) | ~$0.01-0.05/1K messages |
| OpenAI (GPT-5.4 synthesizer) | ~$0.02-0.10/1K responses |
| Cloudflare R2 | Free tier |
| Signal | Free |

---

**Document Version**: 3.0
**Last Updated**: 2026-03-14
