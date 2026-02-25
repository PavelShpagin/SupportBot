#!/bin/bash
ssh -i ~/.ssh/supportbot_ed25519 opc@161.33.64.115 'docker logs supportbot-api --tail 30 2>&1 | cut -c1-200'
