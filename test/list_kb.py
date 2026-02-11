import json

with open('/home/pavel/dev/SupportBot/test/data/streaming_eval/context_kb.json') as f:
    d = json.load(f)

print('KB has', len(d['cases']), 'cases')
print()
for c in d['cases']:
    print(f"{c['idx']}. [{c['status']}] {c['problem_title']}")
    print(f"   Problem: {c['problem_summary'][:100]}...")
    print(f"   Solution: {c['solution_summary'][:100]}...")
    print()
