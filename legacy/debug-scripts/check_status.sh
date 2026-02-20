#!/bin/bash
ssh -i ~/.ssh/supportbot_ed25519 opc@161.33.64.115 << 'EOF'
echo "=== Docker containers ==="
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

echo ""
echo "=== Signal-bot logs (last 10) ==="
docker logs supportbot-api --tail 10 2>&1 | cut -c1-150

echo ""
echo "=== Signal-ingest logs (last 10) ==="
docker logs supportbot-ingest --tail 10 2>&1 | cut -c1-150

echo ""
echo "=== ENV settings ==="
grep -E "SIGNAL_DESKTOP|USE_SIGNAL" ~/SupportBot/.env
EOF
