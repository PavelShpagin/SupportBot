#!/bin/bash
echo "=== Caddy config files ==="
find /home/opc/supportbot -name "Caddyfile" -o -name "*.caddy" 2>/dev/null | xargs head -5 2>/dev/null

echo ""
echo "=== Docker env for domain ==="
docker exec supportbot-caddy env 2>/dev/null | grep -i domain

echo ""
echo "=== All cases (including archived count) ==="
docker exec supportbot-db-1 mysql -u supportbot -psupportbot supportbot -e \
  "SELECT status, count(*) n FROM cases GROUP BY status;"

echo ""
echo "=== Non-archived case IDs ==="
docker exec supportbot-db-1 mysql -u supportbot -psupportbot supportbot -e \
  "SELECT case_id, status, problem_title FROM cases WHERE status IN ('solved','open') ORDER BY created_at DESC;"
