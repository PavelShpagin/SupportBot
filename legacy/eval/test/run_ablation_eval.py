#!/usr/bin/env python3
"""
Ablation Study Evaluation Script

Runs all agents on N=1000 messages to produce comparable results:
    1. DocsOnly   -> large_eval_results.json
    2. ChatRAG    -> chat_search_eval_results.json
    3. DocsChat   -> unified_eval_results.json
    4. SupportBot -> ultimate_eval_results.json
    5. CleanAgent -> clean_eval_results.json
"""

import json
import os
import sys
import time
import argparse
from pathlib import Path
import google.generativeai as genai

# Fix encoding
sys.stdout.reconfigure(encoding='utf-8')

# Get project root
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "test" else SCRIPT_DIR

# Load environment
def _load_env():
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        env_path = Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip("'\"")
        os.environ.setdefault(k, v)

_load_env()
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    raise ValueError("GOOGLE_API_KEY not set")
genai.configure(api_key=GOOGLE_API_KEY)

# Change to project root for proper path resolution
os.chdir(PROJECT_ROOT)


def load_messages(limit=1000):
    """Load incoming messages from signal_messages.json"""
    messages_path = PROJECT_ROOT / "test" / "data" / "signal_messages.json"
    print(f"Loading messages from {messages_path}...")
    
    with open(messages_path, "r", encoding="utf-8") as f:
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
    """Universal judge for all agents"""
    
    def __init__(self, model_name="models/gemini-2.0-flash"):
        self.model = genai.GenerativeModel(model_name)

    def evaluate(self, question, answer, method="general"):
        prompt = f"""You are a quality assurance judge for a support bot.

User Question: {question}
Bot Answer: {answer}

EVALUATION RULES:

1. **BEHAVIOR**:
   - If input is noise/greeting/thanks -> Bot should SKIP (Score 10 if correctly SKIPped)
   - If input is a question -> Bot should ANSWER helpfully

2. **QUALITY**:
   - Helpful answer with citations = Score 9-10
   - Helpful answer without citations = Score 7-8
   - Partial/incomplete answer = Score 5-6
   - Wrong/hallucinated answer = Score 0-3
   - Correctly said "no info" for unanswerable = Score 8-10
   - Missed available info = Score 0-4

3. **SKIP Handling**:
   - Correct SKIP for noise = Score 10
   - Wrong SKIP for real question = Score 0

Output a JSON object:
{{
  "score": (0-10),
  "reasoning": "brief explanation"
}}
"""
        try:
            response = self.model.generate_content(
                prompt, 
                generation_config={"response_mime_type": "application/json"}
            )
            return json.loads(response.text)
        except Exception as e:
            return {"error": str(e), "score": 0}


def run_eval_docsonly(messages, judge):
    """Evaluate DocsOnly agent (gemini_agent)"""
    print("\n" + "="*60)
    print("EVALUATING: DocsOnly (gemini_agent)")
    print("="*60)
    
    from gemini_agent import GeminiAgent, Gate, build_context_from_description
    
    description_path = PROJECT_ROOT / "test" / "description.txt"
    if not description_path.exists():
        description_path = PROJECT_ROOT / "paper" / "repo" / "docs.txt"
    
    context = build_context_from_description(str(description_path))
    agent = GeminiAgent(context)
    gate = Gate(agent)
    
    results = []
    for i, msg in enumerate(messages):
        question = msg.get("body", "").strip()
        print(f"[{i+1}/{len(messages)}] {question[:50]}...", end=" ")
        
        try:
            result = gate.process(question)
            answer = result['response']
            
            eval_result = judge.evaluate(question, answer, "docsonly")
            score = eval_result.get("score", 0)
            
            results.append({
                "query": question,
                "response": answer,
                "method": "DocsOnly",
                "judge_score": score,
                "judge_comment": eval_result.get("reasoning", "")
            })
            print(f"Score: {score}")
            time.sleep(0.5)
        except Exception as e:
            print(f"Error: {e}")
            results.append({
                "query": question,
                "response": f"Error: {e}",
                "method": "DocsOnly",
                "judge_score": 0,
                "judge_comment": "Error during processing"
            })
    
    return results


def run_eval_chatrag(messages, judge):
    """Evaluate ChatRAG agent (chat_search_agent)"""
    print("\n" + "="*60)
    print("EVALUATING: ChatRAG (chat_search_agent)")
    print("="*60)
    
    from chat_search_agent import ChatSearchAgent
    
    index_path = PROJECT_ROOT / "test" / "data" / "chat_index.pkl"
    agent = ChatSearchAgent(str(index_path))
    
    results = []
    for i, msg in enumerate(messages):
        question = msg.get("body", "").strip()
        print(f"[{i+1}/{len(messages)}] {question[:50]}...", end=" ")
        
        try:
            result = agent.answer(question, return_details=True, html_citations=False)
            if isinstance(result, dict):
                answer = result.get("answer", "")
            else:
                answer = result
            
            eval_result = judge.evaluate(question, answer, "chatrag")
            score = eval_result.get("score", 0)
            
            results.append({
                "query": question,
                "response": answer,
                "method": "ChatRAG",
                "judge_score": score,
                "judge_comment": eval_result.get("reasoning", "")
            })
            print(f"Score: {score}")
            time.sleep(0.5)
        except Exception as e:
            print(f"Error: {e}")
            results.append({
                "query": question,
                "response": f"Error: {e}",
                "method": "ChatRAG",
                "judge_score": 0,
                "judge_comment": "Error during processing"
            })
    
    return results


def run_eval_docschat(messages, judge):
    """Evaluate DocsChat agent (unified_agent)"""
    print("\n" + "="*60)
    print("EVALUATING: DocsChat (unified_agent)")
    print("="*60)
    
    from unified_agent import UnifiedAgent
    
    agent = UnifiedAgent()
    
    results = []
    for i, msg in enumerate(messages):
        question = msg.get("body", "").strip()
        print(f"[{i+1}/{len(messages)}] {question[:50]}...", end=" ")
        
        try:
            answer = agent.answer(question)
            
            eval_result = judge.evaluate(question, answer, "docschat")
            score = eval_result.get("score", 0)
            
            results.append({
                "query": question,
                "response": answer,
                "method": "DocsChat",
                "judge_score": score,
                "judge_comment": eval_result.get("reasoning", "")
            })
            print(f"Score: {score}")
            time.sleep(0.5)
        except Exception as e:
            print(f"Error: {e}")
            results.append({
                "query": question,
                "response": f"Error: {e}",
                "method": "DocsChat",
                "judge_score": 0,
                "judge_comment": "Error during processing"
            })
    
    return results


def run_eval_supportbot(messages, judge):
    """Evaluate SupportBot agent (ultimate_agent)"""
    print("\n" + "="*60)
    print("EVALUATING: SupportBot (ultimate_agent)")
    print("="*60)
    
    from ultimate_agent import UltimateAgent
    
    agent = UltimateAgent()
    
    results = []
    for i, msg in enumerate(messages):
        question = msg.get("body", "").strip()
        print(f"[{i+1}/{len(messages)}] {question[:50]}...", end=" ")
        
        try:
            answer = agent.answer(question)
            
            eval_result = judge.evaluate(question, answer, "supportbot")
            score = eval_result.get("score", 0)
            
            results.append({
                "query": question,
                "response": answer,
                "method": "SupportBot",
                "judge_score": score,
                "judge_comment": eval_result.get("reasoning", "")
            })
            print(f"Score: {score}")
            time.sleep(0.5)
        except Exception as e:
            print(f"Error: {e}")
            results.append({
                "query": question,
                "response": f"Error: {e}",
                "method": "SupportBot",
                "judge_score": 0,
                "judge_comment": "Error during processing"
            })
    
    return results


def run_eval_cleanagent(messages, judge):
    """Evaluate CleanAgent (clean_agent)"""
    print("\n" + "="*60)
    print("EVALUATING: CleanAgent (clean_agent)")
    print("="*60)
    
    from clean_agent import CleanAgent
    
    agent = CleanAgent()
    
    results = []
    for i, msg in enumerate(messages):
        question = msg.get("body", "").strip()
        print(f"[{i+1}/{len(messages)}] {question[:50]}...", end=" ")
        
        try:
            answer = agent.answer(question)
            
            eval_result = judge.evaluate(question, answer, "cleanagent")
            score = eval_result.get("score", 0)
            
            results.append({
                "query": question,
                "response": answer,
                "method": "CleanAgent",
                "judge_score": score,
                "judge_comment": eval_result.get("reasoning", "")
            })
            print(f"Score: {score}")
            time.sleep(0.5)
        except Exception as e:
            print(f"Error: {e}")
            results.append({
                "query": question,
                "response": f"Error: {e}",
                "method": "CleanAgent",
                "judge_score": 0,
                "judge_comment": "Error during processing"
            })
    
    return results


def save_results(results, filename):
    """Save results to JSON file"""
    output_path = PROJECT_ROOT / "test" / filename
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(results)} results to {output_path}")


def print_summary(name, results):
    """Print summary statistics"""
    scores = [r['judge_score'] for r in results if 'judge_score' in r]
    if not scores:
        print(f"{name}: No scores")
        return
    
    n = len(scores)
    avg = sum(scores) / n
    acc7 = sum(1 for s in scores if s >= 7) / n * 100
    acc8 = sum(1 for s in scores if s >= 8) / n * 100
    
    print(f"{name:12} N={n:4} Avg={avg:5.2f} Acc>=7={acc7:5.1f}% Acc>=8={acc8:5.1f}%")


def main():
    parser = argparse.ArgumentParser(description="Run ablation study evaluation")
    parser.add_argument("-n", "--limit", type=int, default=1000, help="Number of messages to evaluate")
    parser.add_argument("--agents", type=str, default="all", 
                        help="Comma-separated list of agents: docsonly,chatrag,docschat,supportbot,cleanagent,all")
    parser.add_argument("--resume", action="store_true", help="Resume from existing results")
    args = parser.parse_args()
    
    # Parse agents
    if args.agents == "all":
        agents_to_run = ["docsonly", "chatrag", "docschat", "supportbot", "cleanagent"]
    else:
        agents_to_run = [a.strip().lower() for a in args.agents.split(",")]
    
    print(f"Running ablation study with N={args.limit}")
    print(f"Agents: {', '.join(agents_to_run)}")
    
    # Load messages
    messages = load_messages(args.limit)
    
    # Initialize judge
    judge = Judge()
    
    # Run evaluations
    all_results = {}
    
    if "docsonly" in agents_to_run:
        results = run_eval_docsonly(messages, judge)
        save_results(results, "large_eval_results.json")
        all_results["DocsOnly"] = results
    
    if "chatrag" in agents_to_run:
        results = run_eval_chatrag(messages, judge)
        save_results(results, "chat_search_eval_results.json")
        all_results["ChatRAG"] = results
    
    if "docschat" in agents_to_run:
        results = run_eval_docschat(messages, judge)
        save_results(results, "unified_eval_results.json")
        all_results["DocsChat"] = results
    
    if "supportbot" in agents_to_run:
        results = run_eval_supportbot(messages, judge)
        save_results(results, "ultimate_eval_results.json")
        all_results["SupportBot"] = results
    
    if "cleanagent" in agents_to_run:
        results = run_eval_cleanagent(messages, judge)
        save_results(results, "clean_eval_results.json")
        all_results["CleanAgent"] = results
    
    # Print summary
    print("\n" + "="*60)
    print("ABLATION STUDY RESULTS")
    print("="*60)
    for name, results in all_results.items():
        print_summary(name, results)
    print("="*60)


if __name__ == "__main__":
    main()
