#!/bin/bash
# Sync cases, evidence, and messages from local DB to Oracle VM
set -e

SSH_KEY="${SSH_KEY:-$HOME/.ssh/supportbot_ed25519}"
VM_USER="${VM_USER:-opc}"
VM_HOST="${VM_HOST:-161.33.64.115}"
VM_DB_CONTAINER="${VM_DB_CONTAINER:-supportbot-db}"
LOCAL_DB_CONTAINER="${LOCAL_DB_CONTAINER:-supportbot-db-1}"
EXPORT_FILE="/tmp/supportbot_sync_$(date +%s).sql"

echo "Exporting local cases, evidence, messages..."
docker exec "$LOCAL_DB_CONTAINER" mysqldump \
    -u supportbot -psupportbot \
    --no-create-info --complete-insert --skip-triggers --replace \
    supportbot cases case_evidence raw_messages 2>/dev/null > "$EXPORT_FILE"

ROWS=$(grep -c "^REPLACE" "$EXPORT_FILE" 2>/dev/null || echo "?")
echo "Exported (~$ROWS statements). Uploading to Oracle VM..."

scp -o ConnectTimeout=30 -i "$SSH_KEY" "$EXPORT_FILE" "${VM_USER}@${VM_HOST}:/tmp/sync.sql"

echo "Importing on Oracle VM..."
ssh -o ConnectTimeout=30 -o BatchMode=yes -i "$SSH_KEY" "${VM_USER}@${VM_HOST}" \
    docker exec -i "$VM_DB_CONTAINER" mysql -u supportbot -psupportbot supportbot < /tmp/sync.sql

echo "Sync complete."
rm -f "$EXPORT_FILE"
