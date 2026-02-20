#!/bin/bash
ssh -i ~/.ssh/supportbot_ed25519 opc@161.33.64.115 << 'EOF'
echo "=== SIGNAL-BOT LOGS (last 50) ==="
docker logs supportbot-api --tail 50 2>&1 | cut -c1-200

echo ""
echo "=== SIGNAL-INGEST LOGS (last 30) ==="
docker logs supportbot-ingest --tail 30 2>&1 | cut -c1-200
EOF
