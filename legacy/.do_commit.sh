#!/bin/bash
cd /home/pavel/dev/SupportBot

git add \
  signal-bot/app/db/__init__.py \
  signal-bot/app/db/queries_mysql.py \
  signal-bot/app/jobs/worker.py \
  signal-bot/app/llm/client.py \
  signal-bot/app/llm/prompts.py \
  signal-bot/app/main.py \
  signal-bot/app/r2.py \
  signal-bot/app/signal/signal_cli.py

git commit -m "$(cat <<'EOF'
refactor: simplify case lifecycle and fix reaction parsing

- Fix reaction parsing: skip reaction events in _parse_group_message
- Remove open cases, CLOSE_CASE delay, semantic dedup, span removal
- LLM-driven dedup via overlapping solved cases context
- Fix _load_images for R2 URLs
- Reaction handler: update buffer + enqueue BUFFER_UPDATE
- R2 media cleanup on group removal for compliance
- Remove stale delete_raw_message import
EOF
)"

git push origin main
