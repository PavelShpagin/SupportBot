import json

with open('test/data/streaming_eval/context_kb.json') as f:
    data = json.load(f)

cases = data.get('cases', [])
print(f'Total cases: {len(cases)}')

empty_solution = [c for c in cases if not c.get('solution_summary', '').strip()]
print(f'Cases with empty solution_summary: {len(empty_solution)}')
print()

print('=== Cases with EMPTY solution_summary ===')
for c in empty_solution:
    print(f"Case {c['idx']}: {c['problem_title']}")
    print(f"  Tags: {c.get('tags', [])}")
    print()

print('\n=== Cases with GOOD solution_summary ===')
good_cases = [c for c in cases if c.get('solution_summary', '').strip()]
for c in good_cases[:5]:
    print(f"Case {c['idx']}: {c['problem_title']}")
    print(f"  Solution: {c['solution_summary'][:200]}...")
    print()
