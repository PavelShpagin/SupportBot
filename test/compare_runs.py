#!/usr/bin/env python3
"""Compare all three eval runs"""

import json

runs = {
    'baseline': {
        'answer_pass': 8.7,
        'answer_score': 0.96,
        'answer_respond': 13.0,
        'ignore_pass': 96.8,
        'contains_pass': 81.0,
        'contains_respond': 19.0,
        'overall_pass': 65.3,
        'overall_score': 6.56,
    },
    'run1_clean_buffer': {
        'answer_pass': 13.0,
        'answer_score': 1.83,
        'answer_respond': 30.4,
        'ignore_pass': 87.1,
        'contains_pass': 71.4,
        'contains_respond': 28.6,
        'overall_pass': 60.0,
        'overall_score': 6.16,
    },
    'run2_hybrid': {
        'answer_pass': 13.0,
        'answer_score': 1.83,
        'answer_respond': 30.4,
        'ignore_pass': 87.1,
        'contains_pass': 57.1,
        'contains_respond': 42.9,
        'overall_pass': 56.0,
        'overall_score': 5.76,
    },
}

print("=" * 80)
print("EVALUATION COMPARISON: 3 RUNS")
print("=" * 80)
print()

metrics = [
    ('Answer Pass Rate', 'answer_pass', '%'),
    ('Answer Avg Score', 'answer_score', '/10'),
    ('Answer Respond Rate', 'answer_respond', '%'),
    ('Ignore Pass Rate', 'ignore_pass', '%'),
    ('Contains-Answer Pass', 'contains_pass', '%'),
    ('Contains Respond Rate', 'contains_respond', '%'),
    ('Overall Pass Rate', 'overall_pass', '%'),
    ('Overall Avg Score', 'overall_score', '/10'),
]

print(f"{'Metric':<25} {'Baseline':<12} {'Run1':<12} {'Run2':<12} {'Best'}")
print("-" * 80)

for label, key, unit in metrics:
    baseline = runs['baseline'][key]
    run1 = runs['run1_clean_buffer'][key]
    run2 = runs['run2_hybrid'][key]
    
    best_val = max(baseline, run1, run2)
    
    def fmt(v, best):
        s = f"{v}{unit}"
        return f"{s:<12}" if v != best else f"\033[1;32m{s:<12}\033[0m"
    
    best_run = 'Baseline' if baseline == best_val else ('Run1' if run1 == best_val else 'Run2')
    
    print(f"{label:<25} {fmt(baseline, best_val)} {fmt(run1, best_val)} {fmt(run2, best_val)} {best_run}")

print()
print("=" * 80)
print("KEY FINDINGS")
print("=" * 80)
print()
print("Run 1 (Clean Buffer Only):")
print("  ✅ Contains-answer: 81.0% → 71.4% → still good")
print("  ⚠️  Answer respond: 13% → 30.4% → better but not great")
print()
print("Run 2 (Hybrid Context):")
print("  ❌ Contains-answer: 71.4% → 57.1% → WORSE")
print("  ⚠️  Answer respond: 30.4% → 30.4% → no change")
print()
print("CONCLUSION:")
print("  Baseline has best contains-answer (81%)")
print("  Run1 has best answer respond rate (30.4%)")
print("  Need different approach - hybrid made things worse")
