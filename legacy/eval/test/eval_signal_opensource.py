import json
import os
import sys

# Add parent directory to path if needed for imports
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import google.generativeai as genai
from ultimate_agent import UltimateAgent

# Configuration
DATA_FILE = "test/signal_open_source_data.json"
OUTPUT_FILE = "test/signal_opensource_eval_results.json"
MODEL_NAME = "gemini-2.0-flash"

# Configure Gemini
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    raise ValueError("GOOGLE_API_KEY not set")
genai.configure(api_key=GOOGLE_API_KEY)

def evaluate_answer(question, bot_answer, ground_truth):
    model = genai.GenerativeModel(MODEL_NAME)
    prompt = f"""
    You are an expert judge evaluating a support bot's answer.
    
    Question: {question}
    
    Bot Answer: {bot_answer}
    
    Ground Truth Answer (from GitHub Issue): {ground_truth}
    
    Task:
    Rate the Bot Answer on a scale of 0-10 based on how well it addresses the question compared to the Ground Truth.
    - 10: Perfect match or better (more comprehensive/clear).
    - 7-9: Good answer, covers main points but misses minor details.
    - 4-6: Partial answer, misses key points or is slightly misleading.
    - 0-3: Wrong answer or irrelevant.
    
    Output JSON ONLY:
    {{
        "score": <number>,
        "reasoning": "<string>"
    }}
    """
    try:
        response = model.generate_content(prompt)
        text = response.text.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.endswith("```"):
            text = text[:-3]
        return json.loads(text)
    except Exception as e:
        print(f"Error evaluating: {e}")
        return {"score": 0, "reasoning": "Failed to parse judge response"}

def main():
    if not os.path.exists(DATA_FILE):
        print(f"Data file {DATA_FILE} not found. Please run fetch_signal_data.py first.")
        return

    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    print("Initializing Ultimate Agent...")
    agent = UltimateAgent()
    results = []
    
    print(f"Evaluating {len(data)} cases...")
    
    for i, case in enumerate(data):
        print(f"\nProcessing case {i+1}/{len(data)}...")
        question = case['question']
        ground_truth = case['answer']
        
        # Truncate question for display
        print(f"Q: {question[:100]}...")
        
        try:
            bot_answer = agent.answer(question)
        except Exception as e:
            print(f"Error getting bot answer: {e}")
            bot_answer = "ERROR"
            
        print(f"Bot Answer: {bot_answer[:100]}...")
        
        eval_result = evaluate_answer(question, bot_answer, ground_truth)
        print(f"Score: {eval_result['score']}")
        
        result = {
            "id": case.get('id'),
            "url": case.get('url'),
            "question": question,
            "bot_answer": bot_answer,
            "ground_truth": ground_truth,
            "score": eval_result['score'],
            "reasoning": eval_result['reasoning']
        }
        results.append(result)
        
    # Calculate stats
    if results:
        avg_score = sum(r['score'] for r in results) / len(results)
        print(f"\nAverage Score: {avg_score:.2f}/10")
    else:
        print("\nNo results.")
    
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
        
    print(f"Saved results to {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
