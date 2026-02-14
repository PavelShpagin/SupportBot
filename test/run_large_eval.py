import json
import os
import sys
import time
from pathlib import Path
from gemini_agent import GeminiAgent, Gate, Judge, build_context_from_description

# Fix encoding
sys.stdout.reconfigure(encoding='utf-8')

def load_messages(path, limit=200):
    print(f"Loading messages from {path}...")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    messages = data.get("messages", [])
    # Filter for incoming user messages with text
    incoming = [
        m for m in messages 
        if m.get("type") == "incoming" and m.get("body", "").strip()
    ]
    
    print(f"Total incoming messages: {len(incoming)}")
    
    # Take the last N messages
    selected = incoming[-limit:]
    print(f"Selected last {len(selected)} messages for evaluation.")
    return selected

def main():
    # Load .env (handled by gemini_agent import, but let's be safe)
    if not os.environ.get("GOOGLE_API_KEY"):
        print("GOOGLE_API_KEY not found in env. Please set it.")
        return

    # 1. Build Context
    description_path = "test/description.txt"
    if not os.path.exists(description_path):
        print(f"File not found: {description_path}")
        return

    print("Building context...")
    context = build_context_from_description(description_path)
    
    # 2. Initialize System
    agent = GeminiAgent(context)
    gate = Gate(agent)
    judge = Judge()
    
    # 3. Load Data
    messages_path = "test/data/signal_messages.json"
    if not os.path.exists(messages_path):
        print(f"File not found: {messages_path}")
        return
        
    messages = load_messages(messages_path, limit=500)
    
    # 4. Run Evaluation
    results = []
    total_score = 0
    answered_count = 0
    
    print("\n--- Starting Large Evaluation ---")
    
    for i, msg in enumerate(messages):
        question = msg.get("body", "").strip()
        sender = msg.get("sender", "unknown")
        
        print(f"[{i+1}/{len(messages)}] Processing: {question[:50]}...")
        
        try:
            # Run Gate/Agent
            start_time = time.time()
            result = gate.process(question)
            duration = time.time() - start_time
            
            # Run Judge
            # We judge even if it tagged admin, to see if that was the correct action
            # But the Judge prompt is tuned for "Bot Answer". 
            # If action is tag_admin, the "answer" is the tag message.
            
            eval_result = judge.evaluate(question, result['response'], context)
            score = eval_result.get("score", 0)
            
            # Track stats
            results.append({
                "question": question,
                "sender": sender,
                "action": result['action'],
                "response": result['response'],
                "judge_score": score,
                "judge_reasoning": eval_result.get("reasoning"),
                "duration": duration
            })
            
            total_score += score
            if result['action'] == 'answer':
                answered_count += 1
                
            print(f"  Action: {result['action']}")
            if result['action'] == 'answer':
                print(f"  Response: {result['response'][:100]}...")
            print(f"  Score: {score}")
            
            # Rate limit friendly
            time.sleep(1)
            
        except Exception as e:
            print(f"  Error: {e}")
    
    # 5. Summary
    avg_score = total_score / len(messages) if messages else 0
    print("\n--- Evaluation Summary ---")
    print(f"Total Messages: {len(messages)}")
    print(f"Answered: {answered_count}")
    print(f"Tagged Admin: {len(messages) - answered_count}")
    print(f"Average Quality Score: {avg_score:.2f} / 10")
    
    # Save results
    output_path = "test/large_eval_results.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"Results saved to {output_path}")

if __name__ == "__main__":
    main()
