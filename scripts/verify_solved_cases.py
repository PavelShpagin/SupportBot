#!/usr/bin/env python3
"""Verify that solved cases have solutions."""
import json

with open("test/data/streaming_eval/context_kb.json") as f:
    data = json.load(f)

cases = data.get("cases", [])
solved = [c for c in cases if c.get("status") == "solved"]
open_cases = [c for c in cases if c.get("status") == "open"]

print(f"Total: {len(cases)}")
print(f"Solved: {len(solved)}")
print(f"Open: {len(open_cases)}")
print()

solved_no_sol = [c for c in solved if not c.get("solution_summary", "").strip()]
print(f"Solved cases WITHOUT solution: {len(solved_no_sol)}")
if solved_no_sol:
    for c in solved_no_sol:
        print(f"  - {c.get('problem_title')}")
print()

print("=== Solved cases with solutions (first 5) ===")
for c in solved[:5]:
    sol = c.get("solution_summary", "")[:150]
    print(f"{c.get('idx')}: {c.get('problem_title')}")
    print(f"   Solution: {sol}...")
    print()
