#!/bin/bash
ssh -i ~/.ssh/supportbot_ed25519 opc@161.33.64.115 'docker exec supportbot-api signal-cli -a +380730017651 listContacts 2>&1'
