import json
import os
import sys
import time
from pathlib import Path
from chat_search_agent import ChatSearchAgent
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

    def evaluate(self, question, answer, context_text):
        prompt = f"""
You are a quality assurance judge for a support bot that searches CHAT HISTORY.

CONTEXT (Found in Chat History):
{context_text}

User Question: {question}
Bot Answer: {answer}

EVALUATION RULES:

1. **RELEVANCE**: Did the bot find relevant info in the chat history?
   - If the context contains the answer and the bot used it -> Score 10.
   - If the context contains the answer but the bot missed it -> Score 0.
   - If the context DOES NOT contain the answer, and the bot said "No info found" -> Score 10.
   - If the context DOES NOT contain the answer, but the bot hallucinated -> Score 0.

2. **CITATION**: Did the bot cite the source (Date/Sender)?
   - If yes -> Keep Score.
   - If no -> Deduct 2 points.

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
    index_path = "test/data/chat_index.pkl"
    if not os.path.exists(index_path):
        print(f"Index not found: {index_path}")
        return
        
    agent = ChatSearchAgent(index_path)
    judge = Judge()
    
    # 2. Load Data
    messages_path = "test/data/signal_messages.json"
    messages = load_messages(messages_path, limit=200)
    
    # 3. Run Evaluation
    results = []
    total_score = 0
    
    print("\n--- Starting Chat Search Evaluation ---")
    
    for i, msg in enumerate(messages):
        question = msg.get("body", "").strip()
        sender = msg.get("sender", "unknown")
        
        # Skip very short messages for this specific test to save time/cost and focus on real queries
        if len(question) < 10:
            continue
            
        print(f"[{i+1}/{len(messages)}] Processing: {question[:50]}...")
        
        try:
            # Run Agent
            result = agent.answer(question, return_details=True)
            answer = result["answer"]
            context = result["context"]
            
            # Run Judge
            eval_result = judge.evaluate(question, answer, context)
            score = eval_result.get("score", 0)
            
            # Track stats
            results.append({
                "question": question,
                "answer": answer,
                "context_found": bool(context),
                "judge_score": score,
                "judge_reasoning": eval_result.get("reasoning")
            })
            
            total_score += score
            print(f"  Score: {score}")
            # print(f"  Reasoning: {eval_result.get('reasoning')}")
            
            time.sleep(1)
            
        except Exception as e:
            print(f"  Error: {e}")
    
    # 4. Summary
    avg_score = total_score / len(results) if results else 0
    print("\n--- Evaluation Summary ---")
    print(f"Total Evaluated: {len(results)}")
    print(f"Average Quality Score: {avg_score:.2f} / 10")
    
    # Save results
    output_path = "test/chat_search_eval_results.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"Results saved to {output_path}")

if __name__ == "__main__":
    main()
