#!/usr/bin/env python3
"""Check cases with empty solution_summary and their status."""
import json

with open("test/data/streaming_eval/context_kb.json") as f:
    data = json.load(f)

cases = data.get("cases", [])
empty_sol = [c for c in cases if not c.get("solution_summary", "").strip()]

print(f"Total cases: {len(cases)}")
print(f"Cases with empty solution_summary: {len(empty_sol)}")
print()

for c in empty_sol:
    status = c.get("status", "N/A")
    title = c.get("problem_title", "")[:50]
    print(f"  Case {c.get('idx')}: status={status} - {title}")

print()
print("=== Checking if 'status' field exists at all ===")
has_status = sum(1 for c in cases if "status" in c)
print(f"Cases with 'status' field: {has_status}/{len(cases)}")
