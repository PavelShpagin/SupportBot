#!/bin/bash
ssh -i ~/.ssh/supportbot_ed25519 opc@161.33.64.115 << 'EOF'
echo "=== Link status ==="
curl -s http://localhost:8000/signal/link-device/status | python3 -m json.tool

echo ""
echo "=== Bot logs (last 15 lines) ==="
docker logs supportbot-api --tail 15 2>&1 | cut -c1-150
EOF
