#!/bin/bash
ssh -i ~/.ssh/supportbot_ed25519 opc@161.33.64.115 'docker logs supportbot-api --tail 100 2>&1'
