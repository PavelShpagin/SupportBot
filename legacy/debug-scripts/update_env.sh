#!/bin/bash
# Update .env on Oracle VM to enable Signal Desktop mode
ssh -i ~/.ssh/supportbot_ed25519 opc@161.33.64.115 << 'EOF'
cat >> ~/SupportBot/.env << 'ENVEOF'

# Signal Desktop (for history sync via reverse tunnel)
USE_SIGNAL_DESKTOP=true
SIGNAL_DESKTOP_URL=http://localhost:8002
ENVEOF

echo "Updated .env, restarting signal-ingest..."
cd ~/SupportBot
docker compose -f docker-compose.prod.yml restart signal-ingest
docker logs supportbot-ingest --tail 10 2>&1
EOF
