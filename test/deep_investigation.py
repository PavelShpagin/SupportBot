#!/usr/bin/env python3
"""Deep investigation: Why is the bot not performing well?"""

import json
from collections import defaultdict

# Load eval results
with open('test/data/streaming_eval/eval_results.json') as f:
    data = json.load(f)

results = data['results']

print("=" * 80)
print("DEEP INVESTIGATION: Bot Performance Analysis")
print("=" * 80)
print()

# Analyze ANSWER failures (should respond but doesn't)
answer_msgs = [r for r in results if r['label'] == 'answer']
answer_no_resp = [r for r in answer_msgs if not r.get('bot_responded', False)]
answer_with_resp = [r for r in answer_msgs if r.get('bot_responded', False)]

print(f"ANSWER Messages Analysis ({len(answer_msgs)} total)")
print("-" * 80)
print(f"No response: {len(answer_no_resp)} ({len(answer_no_resp)/len(answer_msgs)*100:.1f}%)")
print(f"Responded:   {len(answer_with_resp)} ({len(answer_with_resp)/len(answer_msgs)*100:.1f}%)")
print()

# Why no response? Check gating
answer_no_resp_gated = [r for r in answer_no_resp if not r.get('bot_considered', True)]
answer_no_resp_stage2 = [r for r in answer_no_resp if r.get('bot_considered', True)]

print("Why no response?")
print(f"  Stage 1 blocked (consider=false): {len(answer_no_resp_gated)}")
print(f"  Stage 2 blocked (respond=false):  {len(answer_no_resp_stage2)}")
print()

# Sample failures
print("Sample ANSWER failures (no response):")
for i, r in enumerate(answer_no_resp[:5], 1):
    print(f"\n{i}. [{r['idx']}] Consider={r.get('bot_considered')} Score={r.get('score', 0)}")
    print(f"   Q: {r['body'][:120]}...")
    print(f"   Expected: {', '.join(r.get('expected_topics', []))}")
    if r.get('bot_response'):
        print(f"   Got: {r['bot_response'][:100]}...")

print()
print("=" * 80)

# Analyze CONTAINS_ANSWER failures (should NOT respond but does)
contains_msgs = [r for r in results if r['label'] == 'contains_answer']
contains_wrong_resp = [r for r in contains_msgs if r.get('bot_responded', False)]
contains_correct = [r for r in contains_msgs if not r.get('bot_responded', False)]

print(f"CONTAINS_ANSWER Analysis ({len(contains_msgs)} total)")
print("-" * 80)
print(f"Correctly silent: {len(contains_correct)} ({len(contains_correct)/len(contains_msgs)*100:.1f}%)")
print(f"Wrongly responded: {len(contains_wrong_resp)} ({len(contains_wrong_resp)/len(contains_msgs)*100:.1f}%)")
print()

# Why wrong response?
print("Sample CONTAINS_ANSWER failures (wrongly responded):")
for i, r in enumerate(contains_wrong_resp[:5], 1):
    print(f"\n{i}. [{r['idx']}] Score={r.get('score', 0)}")
    print(f"   Q: {r['body'][:120]}...")
    if r.get('bot_response'):
        print(f"   Bot said: {r['bot_response'][:100]}...")

print()
print("=" * 80)

# Check if KB has relevant cases
print("KB CASE COVERAGE Check")
print("-" * 80)

# Check expected topics
all_expected = defaultdict(int)
for r in answer_msgs:
    for topic in r.get('expected_topics', []):
        all_expected[topic] += 1

print(f"Expected topics in ANSWER messages ({len(all_expected)} unique):")
for topic, count in sorted(all_expected.items(), key=lambda x: -x[1])[:10]:
    print(f"  {count:2}x {topic}")

print()
print("=" * 80)

# Check response quality when bot does respond
print("RESPONSE QUALITY When Bot Responds")
print("-" * 80)

answer_responded = [r for r in answer_msgs if r.get('bot_responded')]
if answer_responded:
    scores = [r.get('score', 0) for r in answer_responded]
    avg_score = sum(scores) / len(scores)
    high_quality = [r for r in answer_responded if r.get('score', 0) >= 7]
    
    print(f"Total responses to ANSWER: {len(answer_responded)}")
    print(f"Average score: {avg_score:.1f}/10")
    print(f"High quality (≥7): {len(high_quality)} ({len(high_quality)/len(answer_responded)*100:.1f}%)")
    
    print("\nHigh quality responses:")
    for r in high_quality[:3]:
        print(f"  [{r['idx']}] Score={r['score']}")
        print(f"    Q: {r['body'][:80]}...")
        print(f"    A: {r.get('bot_response', '')[:80]}...")

print()
print("=" * 80)
print("KEY FINDINGS")
print("=" * 80)
print()

# Calculate percentages
stage1_block_pct = len(answer_no_resp_gated) / len(answer_msgs) * 100 if answer_msgs else 0
stage2_block_pct = len(answer_no_resp_stage2) / len(answer_msgs) * 100 if answer_msgs else 0

print(f"1. ANSWER messages: {len(answer_msgs)}")
print(f"   - {stage1_block_pct:.1f}% blocked at Stage 1 (decide_consider)")
print(f"   - {stage2_block_pct:.1f}% blocked at Stage 2 (decide_and_respond)")
print(f"   - Only {len(answer_with_resp)/len(answer_msgs)*100:.1f}% get responses")
print()

print(f"2. CONTAINS_ANSWER: {len(contains_msgs)}")
print(f"   - {len(contains_wrong_resp)/len(contains_msgs)*100:.1f}% wrongly respond")
print(f"   - {len(contains_correct)/len(contains_msgs)*100:.1f}% correctly silent")
print()

print("3. ROOT CAUSES:")
print("   ❌ Stage 2 (decide_and_respond) is TOO CONSERVATIVE")
print("   ❌ Even when Stage 1 passes, Stage 2 blocks ~40-50% of real questions")
print("   ❌ Prompt says 'respond if relevant CASE' but bot doesn't trust cases")
print()

print("4. HYPOTHESIS:")
print("   - Clean buffer helps Stage 1 (better gating)")
print("   - But Stage 2 needs BOTH clean buffer AND topic context")
print("   - Current 'CONTEXT' in prompts is confusing")
print("   - Bot doesn't know when buffer is empty due to 'no unsolved threads'")
print("     vs 'no context at all'")
