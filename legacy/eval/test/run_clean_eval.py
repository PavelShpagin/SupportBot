"""
Evaluation script for Clean Agent

Metrics:
- Gate Accuracy: Is noise correctly filtered?
- Retrieval Quality: Are relevant sources found?
- Answer Quality: Is the response helpful and accurate?
- Overall Score: Judge rating 0-10
"""

import json
import os
import sys
from pathlib import Path
import google.generativeai as genai

sys.stdout.reconfigure(encoding='utf-8')

# Load env
def _load_env():
    env_path = Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))

_load_env()
if not os.environ.get("GOOGLE_API_KEY"):
    raise ValueError("GOOGLE_API_KEY not set")

from clean_agent import CleanAgent


class Judge:
    """LLM-based quality judge."""
    
    def __init__(self):
        self.model = genai.GenerativeModel("models/gemini-2.0-flash")
    
    def evaluate(self, question: str, action: str, response: str, gate_result: str, sources: list) -> dict:
        """
        Evaluate agent response.
        
        Scoring:
        - 10: Perfect response (correct action + helpful answer with citations)
        - 7-9: Good response (correct action, mostly helpful)
        - 4-6: Partial (some useful info but incomplete)
        - 1-3: Poor (wrong action or unhelpful)
        - 0: Fail (hallucination, wrong skip, missed answer)
        """
        prompt = f"""You are evaluating a technical support bot's response.

**User Message:** {question[:500]}

**Bot Action:** {action}
**Gate Classification:** {gate_result}
**Sources Used:** {sources}
**Bot Response:** {response[:1000] if response else "None"}

**Evaluation Criteria:**

1. **SKIP Action:**
   - Correct if message is pure noise (greeting, thanks, emoji, off-topic)
   - Incorrect if message contains a question or support request

2. **TAG_ADMIN Action:**
   - Correct if question is legitimate but info truly not available
   - Incorrect if answer could have been synthesized from common knowledge

3. **ANSWER Action:**
   - 10: Accurate, helpful, well-cited
   - 7-9: Mostly correct, some citations
   - 4-6: Partially helpful
   - 0-3: Wrong, hallucinated, or missed the point

**Output JSON:**
{{
  "score": (0-10),
  "reasoning": "Brief explanation",
  "correct_action": true/false
}}"""
        
        try:
            resp = self.model.generate_content(
                prompt,
                generation_config={"response_mime_type": "application/json"}
            )
            return json.loads(resp.text)
        except Exception as e:
            return {"score": 0, "reasoning": f"Judge error: {e}", "correct_action": False}


def load_messages(path: str, limit: int = 200) -> list:
    """Load test messages."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    messages = data.get("messages", [])
    incoming = [m for m in messages if m.get("type") == "incoming" and m.get("body", "").strip()]
    
    print(f"Total incoming: {len(incoming)}, using last {limit}")
    return incoming[-limit:]


def main():
    # Initialize
    agent = CleanAgent()
    judge = Judge()
    
    # Load test data
    messages = load_messages("test/data/signal_messages.json", limit=200)
    
    # Run evaluation
    results = []
    stats = {"SKIP": 0, "ANSWER": 0, "TAG_ADMIN": 0}
    gate_stats = {"SUPPORT": 0, "NOISE": 0, "AMBIGUOUS": 0}
    
    print(f"\n{'='*60}")
    print(f"Running Clean Agent Evaluation on {len(messages)} messages")
    print(f"{'='*60}\n")
    
    for i, msg in enumerate(messages):
        question = msg.get("body", "").strip()
        if len(question) < 2:
            continue
        
        print(f"[{i+1}/{len(messages)}] {question[:60]}...")
        
        # Process
        result = agent.process(question)
        
        # Judge
        eval_result = judge.evaluate(
            question,
            result["action"],
            result["response"],
            result["gate_result"],
            result["sources_used"]
        )
        
        # Record
        results.append({
            "question": question,
            "action": result["action"],
            "response": result["response"],
            "gate_result": result["gate_result"],
            "sources": result["sources_used"],
            "judge_score": eval_result.get("score", 0),
            "judge_reasoning": eval_result.get("reasoning", "")
        })
        
        stats[result["action"]] = stats.get(result["action"], 0) + 1
        gate_stats[result["gate_result"]] = gate_stats.get(result["gate_result"], 0) + 1
        
        print(f"  → {result['action']} | Gate: {result['gate_result']} | Score: {eval_result.get('score', 0)}")
    
    # Summary
    total = len(results)
    avg_score = sum(r["judge_score"] for r in results) / total if total else 0
    acc_7 = sum(1 for r in results if r["judge_score"] >= 7) / total if total else 0
    
    print(f"\n{'='*60}")
    print(f"EVALUATION SUMMARY")
    print(f"{'='*60}")
    print(f"Total: {total}")
    print(f"Actions: SKIP={stats.get('SKIP',0)}, ANSWER={stats.get('ANSWER',0)}, TAG_ADMIN={stats.get('TAG_ADMIN',0)}")
    print(f"Gate: SUPPORT={gate_stats.get('SUPPORT',0)}, NOISE={gate_stats.get('NOISE',0)}, AMBIGUOUS={gate_stats.get('AMBIGUOUS',0)}")
    print(f"Average Score: {avg_score:.2f}/10")
    print(f"Accuracy (>=7): {acc_7*100:.1f}%")
    print(f"{'='*60}\n")
    
    # Save
    output_path = "test/clean_eval_results.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"Results saved to {output_path}")


if __name__ == "__main__":
    main()
