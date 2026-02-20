#!/bin/bash
ssh -i ~/.ssh/supportbot_ed25519 opc@161.33.64.115 'curl -s -o /tmp/bot_qr.png http://localhost:8000/signal/link-device/qr && base64 /tmp/bot_qr.png'
