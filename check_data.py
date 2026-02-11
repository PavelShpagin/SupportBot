#!/usr/bin/env python3
import json
from pathlib import Path

repo = Path(__file__).parent

# Check messages
msg_path = repo / "test/data/signal_messages.json"
if msg_path.exists():
    data = json.loads(msg_path.read_text())
    print(f"Total messages: {len(data.get('messages', []))}")
else:
    print("signal_messages.json not found")

# Check structured cases
cases_path = repo / "test/data/signal_cases_structured.json"
if cases_path.exists():
    data = json.loads(cases_path.read_text())
    print(f"Total structured cases: {len(data.get('cases', []))}")
else:
    print("signal_cases_structured.json not found")
