#!/usr/bin/env python3
"""Debug: show what prompt the LLM receives in eval."""
import json

# Load KB
with open("test/data/streaming_eval/context_kb.json") as f:
    data = json.load(f)
cases = data.get("cases", [])

# Simulate top-5 retrieval (first 5 cases)
retrieved_cases = cases[:5]

# Format as eval does (line 394-402 in run_streaming_eval.py)
group_id = "eval-group"
cases_json = json.dumps([
    {
        "case_id": f"kb-{c.get('idx')}",
        "document": c.get("doc_text", ""),
        "metadata": {"group_id": group_id},
        "distance": None,
    }
    for c in retrieved_cases
], ensure_ascii=False, indent=2)

print("=== Cases JSON passed to decide_and_respond ===")
print(cases_json[:3000])
print()
print("=== Note: metadata does NOT include 'status' or 'solution_summary' ===")
