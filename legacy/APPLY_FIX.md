# How to Apply the QR Timeout Fix

## Quick Summary
The QR code linking was failing because the `signal-ingest` service had a hardcoded 120-second (2 minute) timeout. The fix increases this to 600 seconds (10 minutes) and makes it configurable.

## Steps to Apply

### 1. Update Environment Variables (Optional)
If you want to customize the timeouts, add these to your `.env` file:

```bash
# Signal bot timeout (default: 600 seconds)
SIGNAL_LINK_TIMEOUT_SECONDS=600

# Signal ingest QR scan timeout (default: 600 seconds) - THIS IS THE CRITICAL ONE
HISTORY_QR_TIMEOUT_SECONDS=600

# History sync timeout after scan (default: 300 seconds)
HISTORY_MAX_SECONDS=300
```

**Note**: If you don't add these, the code will use the new defaults (600 seconds for QR scan), which should work fine.

### 2. Rebuild and Restart Services

```bash
cd /home/pavel/dev/SupportBot

# Rebuild the containers with the new code
docker-compose build signal-bot signal-ingest

# Restart the services
docker-compose restart signal-bot signal-ingest
```

Or if you prefer to restart everything:

```bash
docker-compose down
docker-compose up -d
```

### 3. Verify the Fix

Check the logs to confirm the new timeout is being used:

```bash
# Check signal-ingest logs - should show "timeout=600s" instead of "timeout=120s"
docker logs supportbot-ingest --tail 50 | grep "Waiting for Signal Desktop"

# Check signal-bot logs
docker logs supportbot-api --tail 50 | grep -i "link\|timeout"
```

### 4. Test Linking

1. Go to the history bootstrap page
2. Request a QR code for a group
3. You should see the message "Scan this QR code in Signal within 10 minutes"
4. Take your time scanning - you have 10 minutes now!
5. The linking should succeed

## What Changed

### Files Modified:
1. `signal-bot/app/config.py` - Added `signal_link_timeout_seconds` config
2. `signal-bot/app/main.py` - Use config instead of hardcoded 180
3. `signal-bot/app/signal/link_device.py` - Better error messages
4. `signal-bot/app/signal/signal_cli.py` - Updated "60 seconds" to "10 minutes"
5. `signal-ingest/ingest/config.py` - Added `history_qr_timeout_seconds` config
6. `signal-ingest/ingest/main.py` - Use config instead of hardcoded 120 (**critical fix**)
7. `env.example` - Documented new environment variables

## Troubleshooting

### If linking still fails:

1. **Check the timeout in logs:**
   ```bash
   docker logs supportbot-ingest --tail 100 | grep "timeout="
   ```
   Should show `timeout=600s` not `timeout=120s`

2. **Check if environment variables are loaded:**
   ```bash
   docker exec supportbot-ingest env | grep HISTORY_QR_TIMEOUT
   ```

3. **Make sure you rebuilt the containers:**
   ```bash
   docker-compose build --no-cache signal-bot signal-ingest
   docker-compose up -d
   ```

4. **Check Signal Desktop status:**
   ```bash
   docker logs supportbot-desktop --tail 50
   ```

### If you see "QR code scan timed out after XXX seconds":

- If XXX is around 120, the fix wasn't applied - rebuild containers
- If XXX is around 600, you actually did take 10 minutes - try again faster
- The error message now tells you exactly how long you took

## Rollback

If you need to rollback, the old behavior was:
- signal-bot: 180 seconds timeout
- signal-ingest: 120 seconds timeout

You can set these explicitly:
```bash
SIGNAL_LINK_TIMEOUT_SECONDS=180
HISTORY_QR_TIMEOUT_SECONDS=120
```

But this is NOT recommended - the old timeouts were too short.
