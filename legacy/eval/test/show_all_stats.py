import json
import os

script_dir = os.path.dirname(os.path.abspath(__file__))

files = [
    ("large_eval_results.json", "Docs-only"),
    ("chat_search_eval_results.json", "Chat-search"),
    ("unified_eval_results.json", "Unified docs+chat"),
    ("ultimate_eval_results.json", "Ultimate full system"),
    ("clean_eval_results.json", "Clean eval"),
    ("signal_opensource_eval_results.json", "Signal opensource"),
]

print("=" * 60)
print("EVALUATION RESULTS SUMMARY")
print("=" * 60)

for f, name in files:
    try:
        results = json.load(open(os.path.join(script_dir, f)))
        scores = [r.get("judge_score", 0) for r in results]
        avg = sum(scores)/len(scores) if scores else 0
        acc7 = sum(1 for s in scores if s >= 7)/len(scores)*100 if scores else 0
        print(f"{name:30s}: N={len(scores):4d}, Avg={avg:.2f}, Acc>=7={acc7:.1f}%")
    except Exception as e:
        print(f"{name:30s}: Error - {e}")

print("=" * 60)
