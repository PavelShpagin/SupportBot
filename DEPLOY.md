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
|  (Groups/DMs)    |     |   161.33.64.115   |     |  (Gemini 2.5/3)  |
+------------------+     +-------------------+     +------------------+
                               |
                    +----------+----------+
                    |          |          |
              +-----v----+ +---v---+ +----v----+
              | signal-  | | MySQL | | Chroma  |
              | bot:8000 | |  DB   | |  (RAG)  |
              +----------+ +-------+ +---------+
                    |
              +-----v------+
              | signal-    |
              | ingest     |
              +------------+
```

### Services

| Service | Port | Description |
|---------|------|-------------|
| `signal-bot` | 8000 | Main API + Signal CLI listener |
| `signal-ingest` | - | History sync via QR linking |
| `db` | 3306 | MySQL database |
| `rag` | 8001 | ChromaDB vector database |
| `redis` | 6379 | Cache/queue (for scaling) |

---

## Prerequisites

### 1. OCI Account & CLI

```bash
# Install OCI CLI
bash -c "$(curl -L https://raw.githubusercontent.com/oracle/oci-cli/master/scripts/install/install.sh)"

# Configure
oci setup config
```

### 2. SSH Key

```bash
# Generate SSH key for VM access
ssh-keygen -t ed25519 -f ~/.ssh/supportbot_ed25519 -N ""

# Add public key to OCI Console or terraform.tfvars
cat ~/.ssh/supportbot_ed25519.pub
```

### 3. Signal Bot Number

You need a phone number for the bot. Options:
- Use your own number (you already have `+380730017651`)
- Buy a Twilio number (~$1/month)

---

## Deployment Steps

### Step 1: Configure Environment

Edit `.env` with your values:

```bash
# Required
GOOGLE_API_KEY=AIzaSyB...        # Your Gemini API key
SIGNAL_BOT_E164=+380730017651    # Bot phone number

# OCI VM
ORACLE_VM_IP=161.33.64.115       # Your VM's public IP
ORACLE_VM_KEY=~/.ssh/supportbot_ed25519
```

### Step 2: Initialize VM (First Time Only)

If setting up a new VM:

```bash
# Option A: Use existing VM
./scripts/deploy-oci.sh init

# Option B: Create new VM with Terraform
cd infra/oci/terraform
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your values
terraform init
terraform apply
```

### Step 3: Deploy Application

```bash
# Full deployment (push code + build + start)
./scripts/deploy-oci.sh full
```

This will:
1. Sync project files to VM
2. Build Docker images
3. Start all services
4. Run health checks

### Step 4: Link Signal Account

Since you have an existing Signal account on your phone with `+380730017651`:

```bash
./scripts/deploy-oci.sh link-signal
```

This generates a QR code. On your phone:
1. Open Signal
2. Settings > Linked Devices
3. Link New Device
4. Scan the QR code

### Step 5: Set Bot Avatar

```bash
./scripts/deploy-oci.sh set-avatar
```

This sets `supportbot-logo.png` as the bot's profile picture.

---

## Testing the Bot

### 1. Health Check

```bash
# Remote health check
curl http://161.33.64.115:8000/healthz
# Expected: {"ok":true}
```

### 2. Add Bot to a Signal Group

1. Open Signal on your phone
2. Open/create a group
3. Tap group name > Add Members
4. Add `+380730017651`

### 3. Bootstrap Group History

Send a direct message to the bot with the **group name**:
1. Open Signal > New Message > `+380730017651`
2. Type the exact group name (e.g., "Tech Support")
3. Bot sends QR code
4. Scan QR in Signal (Settings > Linked Devices)
5. Bot syncs history and builds knowledge base

### 4. Test Bot Responses

In the group:
- Ask a question: "How do I reset my password?"
- Mention the bot: "@SupportBot what's the wifi password?"

---

## Monitoring

### View Logs

```bash
# All services
./scripts/deploy-oci.sh logs

# Specific service
./scripts/deploy-oci.sh logs signal-bot
```

### Check Status

```bash
./scripts/deploy-oci.sh status
```

### SSH into VM

```bash
./scripts/deploy-oci.sh ssh
```

---

## supportbot.info Website

The landing page at `supportbot.info` is deployed on Vercel.

### Update Website

```bash
cd instructions
vercel --prod
```

The site is at: https://supportbot.info

---

## Troubleshooting

### Bot not responding?

1. Check logs: `./scripts/deploy-oci.sh logs signal-bot`
2. Verify Signal is linked: Look for "receive" messages in logs
3. Check API health: `curl http://161.33.64.115:8000/healthz`

### Signal registration issues?

If registering a new number (not linking existing):

```bash
# SSH into VM
./scripts/deploy-oci.sh ssh

# Inside VM
cd supportbot
docker compose -f docker-compose.prod.yml exec signal-bot bash

# Get captcha from https://signalcaptchas.org/registration/generate.html
signal-cli -a +380730017651 register --captcha "YOUR_CAPTCHA"
signal-cli -a +380730017651 verify "SMS_CODE"
```

### ChromaDB errors?

```bash
# Restart RAG service
./scripts/deploy-oci.sh ssh
cd supportbot
docker compose -f docker-compose.prod.yml restart rag
```

### Database issues?

```bash
# Check MySQL
docker compose -f docker-compose.prod.yml exec db mysql -u supportbot -psupportbot supportbot
```

---

## Security Notes

1. **Firewall**: Port 8000 is restricted to `admin_cidr` in Terraform
2. **Secrets**: Never commit `.env` to git
3. **Signal**: The bot has full access to group messages

---

## Commands Reference

| Command | Description |
|---------|-------------|
| `./scripts/deploy-oci.sh init` | First-time VM setup |
| `./scripts/deploy-oci.sh push` | Push code to VM |
| `./scripts/deploy-oci.sh deploy` | Build & start on VM |
| `./scripts/deploy-oci.sh full` | Push + Deploy |
| `./scripts/deploy-oci.sh ssh` | SSH into VM |
| `./scripts/deploy-oci.sh logs` | View all logs |
| `./scripts/deploy-oci.sh status` | Service status |
| `./scripts/deploy-oci.sh stop` | Stop services |
| `./scripts/deploy-oci.sh restart` | Restart services |
| `./scripts/deploy-oci.sh link-signal` | Link Signal account |
| `./scripts/deploy-oci.sh set-avatar` | Set bot avatar |

---

## Costs

| Resource | Cost |
|----------|------|
| OCI VM (Always Free A1.Flex) | Free |
| OCI Block Storage (50GB free) | Free |
| Google AI (Gemini API) | ~$0.01-0.05/1K messages |
| Signal | Free |
| Vercel (supportbot.info) | Free |

**Estimated monthly cost: $0-5** (mostly Gemini API usage)
