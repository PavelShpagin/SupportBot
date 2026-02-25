#!/bin/bash
SSH="ssh -i ~/.ssh/supportbot_ed25519 -o StrictHostKeyChecking=no opc@161.33.64.115"
DB='docker exec supportbot-db-1 mysql --default-character-set=utf8mb4 -u supportbot -psupportbot supportbot'

case "$1" in
  logs)
    $SSH 'docker compose -f /home/opc/supportbot/docker-compose.yml logs signal-bot --tail=100 2>&1'
    ;;
  gate-logs)
    $SSH 'docker compose -f /home/opc/supportbot/docker-compose.yml logs signal-bot --tail=200 2>&1' | grep -E "Gate:|MAYBE_RESPOND|New solved|New open|SCRAG" | tail -40
    ;;
  cases)
    $SSH "$DB -e 'SELECT case_id, problem_title, status, created_at FROM cases ORDER BY created_at DESC LIMIT 10;' 2>&1"
    ;;
  evidence)
    CID="$2"
    $SSH "$DB -e \"SELECT rm.message_id, LEFT(rm.content_text,100) as txt, rm.image_paths_json FROM raw_messages rm JOIN case_evidence ce ON rm.message_id=ce.message_id WHERE ce.case_id='$CID';\" 2>&1"
    ;;
  case)
    CID="$2"
    $SSH "$DB -e \"SELECT problem_title, problem_summary, solution_summary, status FROM cases WHERE case_id='$CID';\" 2>&1"
    ;;
  groups)
    $SSH "$DB -e 'SELECT DISTINCT group_id FROM cases LIMIT 5;' 2>&1"
    ;;
  commit)
    cd /home/pavel/dev/SupportBot
    git add -A
    git commit -m "$2"
    git push
    ;;
esac
