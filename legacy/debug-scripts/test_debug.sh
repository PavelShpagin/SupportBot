#!/bin/bash
ssh -i ~/.ssh/supportbot_ed25519 opc@161.33.64.115 << 'EOF'
echo "Checking if HTTP_DEBUG_ENDPOINTS_ENABLED is set..."
docker exec supportbot-api env | grep HTTP_DEBUG

echo ""
echo "Testing status endpoint..."
curl -s http://localhost:8000/signal/link-device/status | head -c 200

echo ""
echo ""
echo "Bot logs:"
docker logs supportbot-api --tail 10 2>&1 | cut -c1-150
EOF
