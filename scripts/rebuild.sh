#!/bin/bash
set -e
cd /home/opc/SupportBot
echo "=== Rebuilding signal-bot container ==="
docker compose -f docker-compose.yml build signal-bot --no-cache
echo "=== Restarting signal-bot ==="
docker compose -f docker-compose.yml up -d signal-bot
echo "=== Done! Checking status ==="
docker ps --format "table {{.Names}}\t{{.Status}}"
