#!/bin/bash
ssh -i ~/.ssh/supportbot_ed25519 opc@161.33.64.115 << 'EOF'
echo "Testing from localhost..."
curl -s --connect-timeout 5 http://localhost:8002/status && echo " - OK" || echo " - FAILED"

echo "Testing from 0.0.0.0..."  
curl -s --connect-timeout 5 http://0.0.0.0:8002/status && echo " - OK" || echo " - FAILED"

HOST_IP=$(docker network inspect bridge --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}')
echo "Testing from Docker bridge ($HOST_IP)..."
curl -s --connect-timeout 5 http://$HOST_IP:8002/status && echo " - OK" || echo " - FAILED"

echo ""
echo "Testing from inside ingest container..."
docker exec supportbot-ingest curl -s --connect-timeout 5 http://172.17.0.1:8002/status && echo " - OK" || echo " - FAILED"
EOF
