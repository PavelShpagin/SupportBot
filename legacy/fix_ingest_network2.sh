#!/bin/bash
ssh -i ~/.ssh/supportbot_ed25519 opc@161.33.64.115 << 'EOF'
# Get the host IP from the container's perspective (docker0 gateway)
HOST_IP=$(docker network inspect bridge --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}')
echo "Docker host IP: $HOST_IP"

# Test if we can reach it
echo "Testing from host..."
curl -s --connect-timeout 5 http://$HOST_IP:8002/status && echo "OK from host" || echo "Failed from host"

# The problem is the tunnel binds to localhost only, not all interfaces
# We need to re-establish the tunnel with GatewayPorts

echo ""
echo "The SSH tunnel needs to bind to all interfaces. Killing old tunnel..."
EOF
