import json
import os
import sys
import time
from pathlib import Path
from unified_agent import UnifiedAgent
import google.generativeai as genai

# Fix encoding
sys.stdout.reconfigure(encoding='utf-8')

def load_messages(path, limit=200):
    print(f"Loading messages from {path}...")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    messages = data.get("messages", [])
    incoming = [
        m for m in messages 
        if m.get("type") == "incoming" and m.get("body", "").strip()
    ]
    
    print(f"Total incoming messages: {len(incoming)}")
    selected = incoming[-limit:]
    print(f"Selected last {len(selected)} messages for evaluation.")
    return selected

class Judge:
    def __init__(self, model_name="models/gemini-2.0-flash"):
        self.model = genai.GenerativeModel(model_name)

    def evaluate(self, question, answer):
        prompt = f"""
You are a quality assurance judge for a UNIFIED support bot (Docs + Chat History).

User Question: {question}
Bot Answer: {answer}

EVALUATION RULES:

1. **HELPFULNESS**: Did the bot provide a helpful answer?
   - Yes, from Docs -> Score 10.
   - Yes, from Chat History -> Score 10.
   - No, but correctly Tagged Admin -> Score 10.
   - No, but correctly Skipped (noise) -> Score 10.
   - Hallucinated / Wrong Info -> Score 0.

2. **SYNTHESIS**: Did it combine info well?
   - If it used both sources to give a better answer -> Bonus (keep 10).
   - If it repeated itself or was confusing -> Score 5.

Output a JSON object:
{{
  "score": (0-10),
  "reasoning": "...",
  "correct_action": (true/false)
}}
"""
        try:
            response = self.model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
            return json.loads(response.text)
        except Exception as e:
            return {"error": str(e), "score": 0}

def main():
    if not os.environ.get("GOOGLE_API_KEY"):
        print("GOOGLE_API_KEY not found in env. Please set it.")
        return

    # 1. Initialize System
    agent = UnifiedAgent()
    judge = Judge()
    
    # 2. Load Data
    messages_path = "test/data/signal_messages.json"
    messages = load_messages(messages_path, limit=50) # Start with 50 for Grand Eval
    
    # 3. Run Evaluation
    results = []
    total_score = 0
    
    print("\n--- Starting Unified Evaluation ---")
    
    for i, msg in enumerate(messages):
        question = msg.get("body", "").strip()
        
        # Skip very short messages
        if len(question) < 5:
            continue
            
        print(f"[{i+1}/{len(messages)}] Processing: {question[:50]}...")
        
        try:
            # Run Agent
            answer = agent.answer(question)
            
            # Run Judge
            eval_result = judge.evaluate(question, answer)
            score = eval_result.get("score", 0)
            
            # Track stats
            results.append({
                "question": question,
                "answer": answer,
                "judge_score": score,
                "judge_reasoning": eval_result.get("reasoning")
            })
            
            total_score += score
            print(f"  Score: {score}")
            
            time.sleep(1)
            
        except Exception as e:
            print(f"  Error: {e}")
    
    # 4. Summary
    avg_score = total_score / len(results) if results else 0
    print("\n--- Evaluation Summary ---")
    print(f"Total Evaluated: {len(results)}")
    print(f"Average Quality Score: {avg_score:.2f} / 10")
    
    # Save results
    output_path = "test/unified_eval_results.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"Results saved to {output_path}")

if __name__ == "__main__":
    main()
