#!/bin/bash
ssh -i ~/.ssh/supportbot_ed25519 -o StrictHostKeyChecking=no opc@161.33.64.115 << 'EOF'
echo "=== Message counts ==="
docker exec supportbot-db-1 mysql -u supportbot -psupportbot supportbot -e "SELECT COUNT(*) as total_messages FROM raw_messages"
docker exec supportbot-db-1 mysql -u supportbot -psupportbot supportbot -e "SELECT COUNT(*) as with_images FROM raw_messages WHERE image_paths_json IS NOT NULL AND image_paths_json != '[]'"
docker exec supportbot-db-1 mysql -u supportbot -psupportbot supportbot -e "SELECT COUNT(*) as total_cases FROM cases WHERE status != 'archived'"
echo "=== Groups by message count ==="
docker exec supportbot-db-1 mysql -u supportbot -psupportbot supportbot -e "SELECT group_id, COUNT(*) as n FROM raw_messages GROUP BY group_id ORDER BY n DESC LIMIT 5"
echo "=== Sample image paths ==="
docker exec supportbot-db-1 mysql -u supportbot -psupportbot supportbot -e "SELECT message_id, image_paths_json FROM raw_messages WHERE image_paths_json IS NOT NULL AND image_paths_json != '[]' LIMIT 5"
EOF
