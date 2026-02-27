#!/bin/bash
# Start SSH tunnel to expose local Signal Desktop to remote VM
# 
# This creates a reverse tunnel from the VM's docker bridge (172.17.0.1:8002)
# to the local Signal Desktop service (localhost:8001)
#
# Prerequisites:
# 1. Local Signal Desktop container running on port 8001
# 2. SSH key at ~/.ssh/supportbot_ed25519
# 3. GatewayPorts clientspecified in /etc/ssh/sshd_config on VM

set -e

SSH_KEY="${SSH_KEY:-$HOME/.ssh/supportbot_ed25519}"
VM_HOST="${VM_HOST:-161.33.64.115}"
VM_USER="${VM_USER:-opc}"
LOCAL_PORT="${LOCAL_PORT:-8001}"
REMOTE_PORT="${REMOTE_PORT:-8002}"

# Docker bridge IP on the VM - this is where containers can reach the host
DOCKER_BRIDGE_IP="172.17.0.1"

echo "Starting SSH tunnel: VM:${DOCKER_BRIDGE_IP}:${REMOTE_PORT} -> localhost:${LOCAL_PORT}"

# Kill any existing tunnel
pkill -f "ssh.*${REMOTE_PORT}.*${VM_HOST}" 2>/dev/null || true

# Start new tunnel
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -fNT \
    -R "${DOCKER_BRIDGE_IP}:${REMOTE_PORT}:localhost:${LOCAL_PORT}" \
    "${VM_USER}@${VM_HOST}"

# Verify tunnel
sleep 2
if pgrep -f "ssh.*${REMOTE_PORT}.*${VM_HOST}" > /dev/null; then
    echo "Tunnel started successfully"
    
    # Test from VM
    echo "Testing tunnel from VM..."
    if ssh -i "$SSH_KEY" "${VM_USER}@${VM_HOST}" "curl -s --connect-timeout 5 http://${DOCKER_BRIDGE_IP}:${REMOTE_PORT}/status" | grep -q '"linked"'; then
        echo "Tunnel test PASSED - Signal Desktop is reachable from VM"
    else
        echo "WARNING: Tunnel test failed - Signal Desktop may not be reachable"
    fi
else
    echo "ERROR: Failed to start tunnel"
    exit 1
fi
