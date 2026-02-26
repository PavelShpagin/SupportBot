#!/bin/bash
ssh -i ~/.ssh/supportbot_ed25519 -o StrictHostKeyChecking=no opc@161.33.64.115 << 'EOF'
echo "=== Disk before ==="
df -h /
echo "=== Pruning Docker ==="
docker system prune -af
docker builder prune -af
echo "=== Disk after ==="
df -h /
EOF
