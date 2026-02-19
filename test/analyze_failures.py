"""Analyze low-scoring evaluation results to identify failure patterns."""

import json
import os
from pathlib import Path

script_dir = Path(__file__).parent.resolve()

# Load results
results = json.load(open(script_dir / "ultimate_eval_results.json"))

# Filter low scores
low_scores = [r for r in results if r.get("judge_score", 0) < 7]
print(f"Total evaluated: {len(results)}")
print(f"Low scores (<7): {len(low_scores)}")
print(f"High scores (>=7): {len(results) - len(low_scores)}")
print(f"Accuracy: {(len(results) - len(low_scores)) / len(results) * 100:.1f}%")
print()

print("=" * 60)
print("LOW SCORING EXAMPLES (Score < 7)")
print("=" * 60)

for i, r in enumerate(low_scores[:15]):
    score = r.get("judge_score", 0)
    question = r.get("question", "")[:120]
    answer = str(r.get("answer", ""))[:120]
    reasoning = (r.get("judge_reasoning") or "N/A")[:200]
    
    print(f"\n[{i+1}] Score: {score}")
    print(f"Q: {question}...")
    print(f"A: {answer}...")
    print(f"Reason: {reasoning}...")
    print("-" * 60)

# Count failure patterns
patterns = {
    "context_dependent": 0,  # Needs conversation context
    "attachment_only": 0,     # Just attachment, no text
    "ambiguous": 0,           # Short/unclear question
    "wrong_skip": 0,          # Incorrectly skipped
    "poor_answer": 0,         # Answer given but poor quality
}

for r in low_scores:
    q = r.get("question", "")
    a = str(r.get("answer", ""))
    
    if "[ATTACHMENT" in q and len(q.replace("[ATTACHMENT", "").split("]")[0]) < 30:
        patterns["attachment_only"] += 1
    elif len(q) < 30 and "?" not in q:
        patterns["ambiguous"] += 1
    elif a == "SKIP":
        patterns["wrong_skip"] += 1
    else:
        patterns["poor_answer"] += 1

print("\n" + "=" * 60)
print("FAILURE PATTERNS")
print("=" * 60)
for pattern, count in patterns.items():
    print(f"{pattern}: {count}")
