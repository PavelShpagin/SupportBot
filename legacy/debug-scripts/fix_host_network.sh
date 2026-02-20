#!/bin/bash
ssh -i ~/.ssh/supportbot_ed25519 opc@161.33.64.115 << 'EOF'
cd ~/SupportBot

# Update docker-compose.prod.yml to add network_mode: host to signal-ingest
# First backup
cp docker-compose.prod.yml docker-compose.prod.yml.bak

# Use sed to add network_mode: host after the signal-ingest service definition
# Also need to update SIGNAL_DESKTOP_URL to use localhost since we'll be on host network

# Update .env to use localhost (works with host network)
sed -i 's|SIGNAL_DESKTOP_URL=.*|SIGNAL_DESKTOP_URL=http://localhost:8002|' .env
grep SIGNAL_DESKTOP_URL .env

# Add extra_hosts to the ingest service to map host.docker.internal
# Actually, let's just use a different approach - run ingest with host network

# Check if network_mode already exists
grep -A5 "signal-ingest:" docker-compose.prod.yml | head -10

echo ""
echo "Adding network_mode: host to signal-ingest..."

# Use Python to modify the YAML properly
python3 << 'PYEOF'
import yaml

with open('docker-compose.prod.yml', 'r') as f:
    data = yaml.safe_load(f)

# Add network_mode: host and remove networks from signal-ingest
if 'signal-ingest' in data.get('services', {}):
    data['services']['signal-ingest']['network_mode'] = 'host'
    # Need to change signal-bot URL since we're on host network now
    env = data['services']['signal-ingest'].get('environment', [])
    if isinstance(env, list):
        # Remove old SIGNAL_BOT_URL if present
        env = [e for e in env if not e.startswith('SIGNAL_BOT_URL=')]
        env.append('SIGNAL_BOT_URL=http://localhost:8000')
        data['services']['signal-ingest']['environment'] = env
    # Remove networks since we use host network
    if 'networks' in data['services']['signal-ingest']:
        del data['services']['signal-ingest']['networks']

with open('docker-compose.prod.yml', 'w') as f:
    yaml.dump(data, f, default_flow_style=False, sort_keys=False)

print("Updated docker-compose.prod.yml")
PYEOF

# Recreate the container
docker compose -f docker-compose.prod.yml stop signal-ingest
docker compose -f docker-compose.prod.yml rm -f signal-ingest
docker compose -f docker-compose.prod.yml up -d signal-ingest

sleep 3
echo ""
echo "Testing connectivity..."
docker exec supportbot-ingest curl -s --connect-timeout 5 http://localhost:8002/status && echo " - OK" || echo " - FAILED (container might use host network directly)"

# With host network, curl from container IS the same as from host
echo ""
echo "Ingest logs:"
docker logs supportbot-ingest --tail 5 2>&1
EOF
