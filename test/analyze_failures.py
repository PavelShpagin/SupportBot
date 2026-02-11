import json

with open('test/data/streaming_eval/eval_results.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

results = data['results']

# Analyze failures by category
print('=== FAILURE ANALYSIS ===\n')

# 1. Answer label failures (should respond but didn't, or responded poorly)
answer_fails = [r for r in results if r['label'] == 'answer' and not r['judge_passed']]
print(f'1. ANSWER FAILURES: {len(answer_fails)}/23')
print(f'   - Bot considered: {sum(1 for r in answer_fails if r["consider"])}')
print(f'   - Bot responded: {sum(1 for r in answer_fails if r["responded"])}')
print(f'   - Bot considered but not responded: {sum(1 for r in answer_fails if r["consider"] and not r["responded"])}')
print(f'   - Bot not considered at all: {sum(1 for r in answer_fails if not r["consider"])}')

# Show top 3 examples
print('\n   Top 3 examples:')
for i, r in enumerate(answer_fails[:3], 1):
    print(f'   {i}. idx={r["idx"]}, consider={r["consider"]}, responded={r["responded"]}')
    print(f'      body: {r["body"][:80]}...')
    print(f'      reason: {r["judge_reasoning"][:100]}')
    print()

# 2. Ignore label failures (should stay silent but responded)
ignore_fails = [r for r in results if r['label'] == 'ignore' and not r['judge_passed']]
print(f'2. IGNORE FAILURES: {len(ignore_fails)}/31')
print(f'   - Bot responded when it should not: {sum(1 for r in ignore_fails if r["responded"])}')

print('\n   Examples:')
for i, r in enumerate(ignore_fails[:3], 1):
    print(f'   {i}. idx={r["idx"]}, responded={r["responded"]}')
    print(f'      body: {r["body"][:80]}...')
    print(f'      reason: {r["judge_reasoning"][:100]}')
    print()

# 3. Contains-answer failures (should stay silent but responded)
contains_fails = [r for r in results if r['label'] == 'contains_answer' and not r['judge_passed']]
print(f'3. CONTAINS_ANSWER FAILURES: {len(contains_fails)}/21')
print(f'   - Bot responded when answer already present: {sum(1 for r in contains_fails if r["responded"])}')

print('\n   Examples:')
for i, r in enumerate(contains_fails[:3], 1):
    print(f'   {i}. idx={r["idx"]}, responded={r["responded"]}')
    print(f'      body: {r["body"][:80]}...')
    print(f'      reason: {r["judge_reasoning"][:100]}')
    print()
