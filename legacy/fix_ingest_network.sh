#!/bin/bash
ssh -i ~/.ssh/supportbot_ed25519 opc@161.33.64.115 << 'EOF'
cd ~/SupportBot

# Update the .env to use host.docker.internal or the docker host gateway
# First, remove the old SIGNAL_DESKTOP_URL line and add the correct one
sed -i '/SIGNAL_DESKTOP_URL/d' .env
echo "SIGNAL_DESKTOP_URL=http://host.docker.internal:8002" >> .env

# Check what we have now
grep -E "SIGNAL_DESKTOP|USE_SIGNAL" .env

# Recreate the ingest container
docker compose -f docker-compose.prod.yml stop signal-ingest
docker compose -f docker-compose.prod.yml rm -f signal-ingest
docker compose -f docker-compose.prod.yml up -d signal-ingest

sleep 3
# Test connectivity from inside the container
echo ""
echo "Testing from inside container..."
docker exec supportbot-ingest curl -s --connect-timeout 5 http://host.docker.internal:8002/status || echo "host.docker.internal failed"
EOF
