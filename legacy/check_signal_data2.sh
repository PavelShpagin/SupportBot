#!/bin/bash
ssh -i ~/.ssh/supportbot_ed25519 opc@161.33.64.115 'docker exec supportbot-api ls -la /var/lib/signal/bot/data/'
