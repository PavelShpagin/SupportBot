#!/usr/bin/env python3
"""
Analyze why the bot failed to respond to 'answer' labeled questions.
Check if the actual chat history contains answers that case extraction missed.
"""
import json

# Load eval results
with open("test/data/streaming_eval/eval_results.json") as f:
    results = json.load(f)

# Load eval messages (with full chat context)
with open("test/data/streaming_eval/eval_messages_labeled.json") as f:
    eval_data = json.load(f)

messages = eval_data.get("messages", [])
msg_by_idx = {m["idx"]: m for m in messages}

# Load KB cases
with open("test/data/streaming_eval/context_kb.json") as f:
    kb_data = json.load(f)
cases = kb_data.get("cases", [])

print("="*80)
print("ANALYSIS: Why did the bot fail to respond to 'answer' questions?")
print("="*80)
print()

# Find failed 'answer' questions
failed_answers = [
    r for r in results["results"]
    if r.get("label") == "answer" and not r.get("responded")
]

print(f"Total 'answer' questions: {sum(1 for r in results['results'] if r.get('label')=='answer')}")
print(f"Bot responded to: {sum(1 for r in results['results'] if r.get('label')=='answer' and r.get('responded'))}")
print(f"Bot FAILED to respond: {len(failed_answers)}")
print()

# Analyze each failed answer
for r in failed_answers[:10]:  # First 10
    idx = r["idx"]
    body = r["body"][:150] + "..." if len(r["body"]) > 150 else r["body"]
    expected = r.get("expected_topics", [])
    
    print(f"--- Message {idx} ---")
    print(f"Question: {body}")
    print(f"Expected topics: {expected}")
    print(f"Retrieved cases: {r.get('retrieved_cases_count', 0)}")
    print(f"Consider gate: {r.get('consider', 'N/A')}")
    
    # Check if any KB case matches the expected topics
    matching_cases = []
    for c in cases:
        tags = " ".join(c.get("tags", [])).lower()
        title = c.get("problem_title", "").lower()
        for topic in expected:
            if topic.lower() in tags or topic.lower() in title:
                matching_cases.append(c)
                break
    
    if matching_cases:
        print(f"Potentially matching KB cases: {len(matching_cases)}")
        for mc in matching_cases[:2]:
            sol = mc.get("solution_summary", "")
            has_sol = "YES" if sol.strip() else "NO (EMPTY!)"
            print(f"  - Case {mc['idx']}: {mc['problem_title'][:50]}... [has solution: {has_sol}]")
    else:
        print(f"NO matching KB cases found for expected topics!")
    
    print()
