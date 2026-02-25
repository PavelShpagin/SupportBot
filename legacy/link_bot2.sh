#!/bin/bash
# Generate a new linking QR for signal-bot
ssh -i ~/.ssh/supportbot_ed25519 opc@161.33.64.115 'curl -v http://localhost:8000/signal/link-device/qr 2>&1 | head -30'
