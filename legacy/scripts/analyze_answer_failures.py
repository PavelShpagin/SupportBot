#!/usr/bin/env python3
"""Analyze why bot doesn't respond to 'answer' labeled messages."""
import json

with open("test/data/streaming_eval/eval_results.json") as f:
    data = json.load(f)

results = data["results"]
answer_msgs = [r for r in results if r["label"] == "answer"]

print(f"Total answer-labeled messages: {len(answer_msgs)}")
responded = sum(1 for r in answer_msgs if r.get("responded"))
not_responded = sum(1 for r in answer_msgs if not r.get("responded"))
print(f"Responded: {responded}")
print(f"Did NOT respond: {not_responded}")
print()

# Analyze why bot didn't respond
considered_but_no_respond = []
not_considered = []

for r in answer_msgs:
    if not r.get("responded"):
        if r.get("consider"):
            considered_but_no_respond.append(r)
        else:
            not_considered.append(r)

print(f"Breakdown of non-responses:")
print(f"  - Considered but didn't respond: {len(considered_but_no_respond)}")
print(f"  - Not even considered (gate rejected): {len(not_considered)}")
print()

print("=== Messages CONSIDERED but bot didn't respond (first 5) ===")
for r in considered_but_no_respond[:5]:
    print(f"Msg {r['idx']}: {r['body'][:120]}...")
    print(f"  Retrieved cases: {r.get('retrieved_cases_count')}")
    print()

print("=== Messages NOT CONSIDERED by gate (first 5) ===")
for r in not_considered[:5]:
    print(f"Msg {r['idx']}: {r['body'][:120]}...")
    print()
