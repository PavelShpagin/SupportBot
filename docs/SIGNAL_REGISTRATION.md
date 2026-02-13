# Signal registration & persistence (prevent re-linking)

This project uses **`signal-cli` inside Docker**. The "Signal registration/linking" state is just files on disk under `SIGNAL_BOT_STORAGE` (default: `/var/lib/signal/bot`).

If those files are lost/emptied, Signal will show the device as unregistered and you'll have to link again.

---

## How to know the bot is fully ready

The system is "ready" when all of these are true:

- Docker services are up:

```bash
docker compose -f docker-compose.prod.yml ps
```

- API health is OK:

```bash
curl http://localhost:8000/healthz
```

- Signal account is linked (and the receive loop is running):

```bash
curl http://localhost:8000/signal/link-device/status
```

- History ingestion worker is running:

```bash
docker compose -f docker-compose.prod.yml logs --tail=50 signal-ingest
```

To confirm **chat responding**, send a test message in a connected group that mentions the bot (see `BOT_MENTION_STRINGS`, e.g. `@supportbot`) and verify a response arrives.

---

## Prevent re-registration / re-linking

### 1) Persist Signal storage on the host (bind mounts)

Use **bind mounts** for Signal state so it can't disappear due to Docker volume renames.

This repo's `docker-compose.yml` and `docker-compose.prod.yml` mount:

- `/var/lib/signal/bot` → main bot account state
- `/var/lib/signal/ingest` → ingestion/link-device state used for history bootstrap
- `/var/lib/history` → QR images + history artifacts

**Do not delete these directories.**

### 2) Never run "delete volumes" commands

Avoid commands that wipe persistent storage:

- `docker compose down -v`
- `docker system prune --volumes`

### 3) Don't unlink the device from your phone

If you remove the linked device in Signal (**Settings → Linked devices**), you will have to link again. That's expected Signal behavior.

### 4) Back up the Signal state

Recommended backup (run on the host):

```bash
sudo mkdir -p /var/backups/supportbot
sudo tar -C /var/lib -czf /var/backups/supportbot/signal_$(date +%Y%m%d_%H%M%S).tgz signal
sudo tar -C /var/lib -czf /var/backups/supportbot/history_$(date +%Y%m%d_%H%M%S).tgz history
```

Restore example:

```bash
sudo tar -C /var/lib -xzf /var/backups/supportbot/signal_YYYYMMDD_HHMMSS.tgz
sudo tar -C /var/lib -xzf /var/backups/supportbot/history_YYYYMMDD_HHMMSS.tgz
docker compose -f docker-compose.prod.yml up -d
```

---

## How to link the bot (QR) safely

This repo includes a built-in QR endpoint (debug-only) that:
- generates a QR PNG
- keeps `signal-cli link` running until the scan completes

### 1) Temporarily enable debug endpoints

Set in `.env`:

```bash
HTTP_DEBUG_ENDPOINTS_ENABLED=true
```

Restart the bot:

```bash
docker compose -f docker-compose.prod.yml up -d --build signal-bot
```

### 2) Scan the QR from a desktop screen

Open on **your desktop** (so your phone can scan it):

- QR: `http://<server>:8000/signal/link-device/qr`
- Status: `http://<server>:8000/signal/link-device/status`

Scan path on phone:
- Signal → Settings → Linked devices → Link new device

When status becomes `linked`, the bot will start the receive loop automatically.

### 3) Disable debug endpoints again (recommended)

Set:

```bash
HTTP_DEBUG_ENDPOINTS_ENABLED=false
```

Restart:

```bash
docker compose -f docker-compose.prod.yml up -d signal-bot
```

---

## History ingestion with Signal Desktop (recommended)

This repo now supports running **Signal Desktop headlessly** on the VM. This enables real 45-day history transfer - unlike signal-cli, Signal Desktop actually receives historical messages when you link.

### Enable Signal Desktop mode

Set in `.env`:

```bash
USE_SIGNAL_DESKTOP=true
```

Restart services:

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

### How it works

1. `signal-desktop` container runs Signal Desktop with Xvfb (virtual display)
2. When you link, Signal Desktop receives the 45-day history transfer
3. `signal-ingest` queries the Signal Desktop SQLite DB for messages
4. Messages are processed into cases and added to the knowledge base

### Initial linking with Signal Desktop

1. Make sure `signal-desktop` container is running:
   ```bash
   docker compose -f docker-compose.prod.yml logs -f signal-desktop
   ```

2. Get a screenshot of the QR code (during first-time setup):
   ```bash
   curl http://localhost:8001/screenshot > qr.png
   ```
   Or open `http://<server>:8001/screenshot` in a browser.

3. Scan the QR code from your phone:
   - Signal → Settings → Linked devices → Link new device
   - Choose "Transfer message history" when prompted

4. Wait for sync to complete (check status):
   ```bash
   curl http://localhost:8001/status
   ```

5. Trigger history ingestion by DM'ing the bot with the group name.

### Resource usage

Signal Desktop requires more resources than signal-cli:
- ~500MB RAM for Signal Desktop process
- ~2GB shared memory (set via `shm_size: 2gb` in docker-compose)

---

## Fallback: signal-cli mode

If you can't run Signal Desktop (e.g., ARM architecture, limited resources), you can use the legacy signal-cli mode:

```bash
USE_SIGNAL_DESKTOP=false
```

**Limitation**: signal-cli does NOT receive the 45-day history transfer. It only captures new messages that arrive while `signal-cli receive` is running.

---

## Offline history extraction (manual)

For one-time imports, you can extract history from Signal Desktop on your local machine:

1. **Extract the key** (on Windows with Signal Desktop):
   ```bash
   python test/decrypt_key_win.py
   ```
   This saves `signal_key.txt` to your Desktop.

2. **Copy the key and extract the backup**:
   ```bash
   # Copy signal_key.txt to test/secrets/signal_key.txt
   # Extract your Signal Desktop backup to test/data/extracted/Signal1/
   ```

3. **Export messages**:
   ```bash
   python test/read_signal_db.py
   ```

4. **Mine cases**:
   ```bash
   python test/mine_real_cases.py
   ```

This produces structured cases in `test/data/signal_cases_structured.json` that can be imported into the bot's knowledge base.
