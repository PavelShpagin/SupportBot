#!/bin/bash
ssh -i ~/.ssh/supportbot_ed25519 opc@161.33.64.115 'docker exec supportbot-ingest env | grep -iE "use_signal|desktop_url"'
