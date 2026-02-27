#!/bin/bash
ssh -o ConnectTimeout=120 -o ServerAliveInterval=10 -o BatchMode=yes -i ~/.ssh/supportbot_ed25519 opc@161.33.64.115 'cd /home/opc/SupportBot && docker compose -f docker-compose.yml build signal-web && docker compose -f docker-compose.yml up -d signal-web && echo BUILD_OK'
