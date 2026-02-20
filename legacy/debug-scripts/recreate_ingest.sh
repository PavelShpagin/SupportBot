#!/bin/bash
ssh -i ~/.ssh/supportbot_ed25519 opc@161.33.64.115 << 'EOF'
cd ~/SupportBot
# Stop and remove the old container
docker compose -f docker-compose.prod.yml stop signal-ingest
docker compose -f docker-compose.prod.yml rm -f signal-ingest
# Recreate with new env
docker compose -f docker-compose.prod.yml up -d signal-ingest
# Wait a bit and check
sleep 5
docker exec supportbot-ingest env | grep -iE "use_signal|desktop_url"
docker logs supportbot-ingest --tail 5 2>&1
EOF
