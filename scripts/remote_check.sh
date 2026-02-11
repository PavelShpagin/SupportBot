#!/bin/bash
echo "=== Docker Containers ==="
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
echo ""
echo "=== Health Check ==="
curl -sf http://localhost:8000/healthz && echo " OK" || echo "API not responding"
echo ""
echo "=== Logs (last 10 lines) ==="
cd /home/opc/SupportBot
docker compose -f docker-compose.prod.yml logs --tail=10 signal-bot 2>/dev/null
