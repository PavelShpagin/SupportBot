#!/bin/bash
# Daily keepalive: sends a message to the "testx" group via signal-cli
# to prevent Signal from unlinking the bot due to inactivity.
# Runs as a cron job on the Oracle VM host.

set -euo pipefail

CONTAINER="supportbot-signal-bot-1"
SIGNAL_CONFIG="/var/lib/signal/bot"
TESTX_GROUP_ID="X6s1X1dtQJ/d0cPUFskQtzfM3e9TGcxQi3Dg8CDXZTs="
LOG_FILE="/var/log/signal_keepalive.log"

TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
MSG="keepalive $(date '+%Y-%m-%d')"

echo "[$TIMESTAMP] Sending keepalive to testx..." >> "$LOG_FILE"

if docker exec "$CONTAINER" signal-cli --config "$SIGNAL_CONFIG" send \
    -g "$TESTX_GROUP_ID" \
    -m "$MSG" >> "$LOG_FILE" 2>&1; then
    echo "[$TIMESTAMP] OK" >> "$LOG_FILE"
else
    echo "[$TIMESTAMP] FAILED (exit $?)" >> "$LOG_FILE"
fi
