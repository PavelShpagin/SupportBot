#!/bin/bash
ssh -i ~/.ssh/supportbot_ed25519 opc@161.33.64.115 << 'EOF'
cd ~/SupportBot

# Add debug endpoints env var if not present
grep -q "HTTP_DEBUG_ENDPOINTS_ENABLED" .env || echo -e "\n# Enable debug endpoints for linking\nHTTP_DEBUG_ENDPOINTS_ENABLED=true" >> .env

# Restart the API to pick up the change
docker compose -f docker-compose.prod.yml stop signal-bot
docker compose -f docker-compose.prod.yml rm -f signal-bot
docker compose -f docker-compose.prod.yml up -d signal-bot

sleep 5
echo "Testing endpoint..."
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/signal/link-device/status
EOF
