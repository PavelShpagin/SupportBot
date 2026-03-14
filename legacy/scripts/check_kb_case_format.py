#!/usr/bin/env python3
"""Check KB case doc_text format."""
import json

with open("test/data/streaming_eval/context_kb.json") as f:
    data = json.load(f)

cases = data.get("cases", [])
solved = [c for c in cases if c.get("status") == "solved"]
open_cases = [c for c in cases if c.get("status") == "open"]

print(f"Total cases: {len(cases)}")
print(f"Solved: {len(solved)}, Open: {len(open_cases)}")
print()

print("=== Sample SOLVED case ===")
for c in solved[:1]:
    print(f"Status: {c.get('status')}")
    print(c.get("doc_text", ""))
    print("---")

print()
print("=== Sample OPEN case ===")
for c in open_cases[:1]:
    print(f"Status: {c.get('status')}")
    print(c.get("doc_text", ""))
    print("---")
