#!/bin/bash
cd /home/pavel/dev/SupportBot
source .venv/bin/activate

# Load .env
export $(grep -v '^#' .env | xargs)

echo "API KEY prefix: ${GOOGLE_API_KEY:0:10}..."
python -u test/run_ultimate_eval.py
