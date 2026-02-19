import json
import os
import sys
import time
from pathlib import Path
from ultimate_agent import UltimateAgent
import google.generativeai as genai

# Fix encoding
sys.stdout.reconfigure(encoding='utf-8')

# Get project root
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "test" else SCRIPT_DIR

def load_messages(path, limit=200):
    # Handle relative paths
    if not Path(path).is_absolute():
        path = PROJECT_ROOT / path
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
You are a quality assurance judge for an ULTIMATE support bot (Docs + Cases + Chat History).

User Question: {question}
Bot Answer: {answer}

EVALUATION RULES:

1. **BEHAVIOR**:
   - If input is noise/greeting -> Bot should SKIP (Score 10).
   - If input is a question -> Bot should ANSWER or TAG ADMIN.

2. **QUALITY**:
   - If Answered: Is it helpful and does it cite sources? (Score 10)
   - If Tagged Admin: Was it truly unanswerable from standard docs/cases? (Score 10)
   - If Hallucinated: Score 0.
   - If Missed Info: Score 0.

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
    agent = UltimateAgent()
    judge = Judge()
    
    # 2. Load Data
    messages_path = "test/data/signal_messages.json"
    messages = load_messages(messages_path, limit=200)
    
    # 3. Run Evaluation
    results = []
    total_score = 0
    skipped_count = 0
    answered_count = 0
    tagged_count = 0
    
    print("\n--- Starting Ultimate Evaluation (200 Messages) ---")
    
    for i, msg in enumerate(messages):
        question = msg.get("body", "").strip()
        
        # Skip very short messages to save time, unless they look like specific keywords
        if len(question) < 3:
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
            if answer == "SKIP":
                skipped_count += 1
            elif "INSUFFICIENT_INFO" in answer or "@admin" in answer:
                tagged_count += 1
            else:
                answered_count += 1
                
            print(f"  Result: {answer[:100]}...")
            print(f"  Score: {score}")
            
            # time.sleep(1) # Remove sleep to speed up
            
        except Exception as e:
            print(f"  Error: {e}")
    
    # 4. Summary
    avg_score = total_score / len(results) if results else 0
    print("\n--- Evaluation Summary ---")
    print(f"Total Evaluated: {len(results)}")
    print(f"Skipped: {skipped_count}")
    print(f"Answered: {answered_count}")
    print(f"Tagged Admin: {tagged_count}")
    print(f"Average Quality Score: {avg_score:.2f} / 10")
    
    # Save results
    output_path = PROJECT_ROOT / "test" / "ultimate_eval_results.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"Results saved to {output_path}")

if __name__ == "__main__":
    main()
