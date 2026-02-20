import json
import os
import sys

# Add test directory to path to import UltimateAgent
# Assuming we run from root: python paper/scripts/evaluate_dataset.py
sys.path.append(os.path.abspath("test"))

import google.generativeai as genai
try:
    from ultimate_agent import UltimateAgent
except ImportError:
    # Fallback if running from paper/scripts
    sys.path.append(os.path.abspath("../../test"))
    from ultimate_agent import UltimateAgent

# Configuration
DATA_FILE = "paper/data/signal_eval_dataset.json"
OUTPUT_FILE = "paper/data/signal_eval_results.json"
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
    
    Ground Truth Answer (from Community Forum): {ground_truth}
    
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
        print(f"Data file {DATA_FILE} not found. Please run process_dataset.py first.")
        return

    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    print("Initializing Ultimate Agent...")
    try:
        agent = UltimateAgent()
    except Exception as e:
        print(f"Failed to initialize agent: {e}")
        return

    results = []
    
    # Filter only cases with expected output
    eval_cases = [c for c in data if c['expected_output']['has_solution']]
    print(f"Evaluating {len(eval_cases)} cases (filtered from {len(data)} total)...")
    
    for i, case in enumerate(eval_cases):
        print(f"\nProcessing case {i+1}/{len(eval_cases)}...")
        question = case['input_message']['content']
        ground_truth = case['expected_output']['solution_content']
        
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
            "url": case['context'].get('thread_url'),
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
    
    # Ensure output directory exists
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
        
    print(f"Saved results to {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
