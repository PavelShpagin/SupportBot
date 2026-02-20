#!/bin/bash
ssh -i ~/.ssh/supportbot_ed25519 -o StrictHostKeyChecking=no opc@161.33.64.115 'curl -s -o /tmp/desktop_qr.png "http://localhost:8001/screenshot?crop_qr=true" && base64 /tmp/desktop_qr.png'
