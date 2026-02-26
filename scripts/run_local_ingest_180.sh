#!/bin/bash
# Run local history ingestion with ~180 messages, expect 4 cases.
# Usage: ./scripts/run_local_ingest_180.sh
# Requires: GOOGLE_API_KEY in env or .env

set -e
cd "$(dirname "$0")/.."

# Load .env if present (Python script also loads it, but we need it for the check)
[ -f .env ] && set -a && source .env && set +a

if [ -z "$GOOGLE_API_KEY" ]; then
  echo "GOOGLE_API_KEY not set. Export it or add to .env"
  exit 1
fi

echo "Running local ingest: ~180 messages, --local --post-to-prod (working links)"
TARGET_MESSAGES=180 python3 legacy/run_local_ingest.py --local --post-to-prod
