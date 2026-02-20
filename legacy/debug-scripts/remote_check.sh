#!/bin/bash
# Check recent logs and DB state
echo "=== Signal-Ingest Logs ==="
docker logs supportbot-ingest --tail 50 2>&1

echo ""
echo "=== Signal-Bot Recent QR/Group Activity ==="
docker logs supportbot-api --tail 1000 2>&1 | grep -iE 'group|qr|enqueue|HISTORY' | tail -30

echo ""
echo "=== Job Queue ==="
docker exec supportbot-db mysql -u root -prootpassword supportbot -e "SELECT id, type, status, created_at FROM job_queue ORDER BY id DESC LIMIT 10;" 2>/dev/null

echo ""
echo "=== History Directory ==="
ls -la /var/lib/history/ 2>/dev/null || echo "History dir not found or empty"
