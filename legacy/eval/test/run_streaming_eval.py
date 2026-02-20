#!/usr/bin/env python3
"""
Run streaming evaluation using pre-prepared dataset.

Uses:
- test/data/streaming_eval/context_kb.json (cached KB from 400 messages)
- test/data/streaming_eval/eval_messages_labeled.json (labeled 100 messages)

For each eval message:
1. Based on label, determine expected behavior:
   - "ignore": bot should NOT respond
   - "answer": bot SHOULD respond; judge quality
   - "contains_answer": bot should NOT respond (answer already given)
2. Run SupportBot's decision + respond pipeline
3. Use Gemini judge to evaluate:
   - For "answer" labels: quality (0-10), usefulness, conciseness, factual correctness
   - For "ignore"/"contains_answer": check bot correctly stayed silent

Outputs (test/data/streaming_eval/):
- eval_results.json: detailed results for each message
- eval_summary.json: aggregated metrics

Usage:
  source .venv/bin/activate
  python test/run_streaming_eval.py

Env vars:
- JUDGE_MODEL: model for evaluation (default: gemini-2.5-flash-lite)
- STREAMING_EVAL_TOP_K: number of KB cases to retrieve (default: 5)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI


def _now_tag() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _maybe_load_dotenv(dotenv_path: Path) -> None:
    """Load key=value pairs from .env, stripping CRLF."""
    if not dotenv_path.exists():
        return
    for raw in dotenv_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip("\r")
        if not k:
            continue
        if (v.startswith("'") and v.endswith("'")) or (v.startswith('"') and v.endswith('"')):
            v = v[1:-1]
        os.environ.setdefault(k, v)


def _dot(a: List[float], b: List[float]) -> float:
    """Dot product for cosine similarity (assuming normalized vectors)."""
    return float(sum(x * y for x, y in zip(a, b)))


def _topk_cases(cases: List[Dict[str, Any]], query_emb: List[float], k: int) -> List[Dict[str, Any]]:
    """Retrieve top-k cases by embedding similarity."""
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for c in cases:
        emb = c.get("embedding") or []
        if not emb:
            continue
        scored.append((_dot(query_emb, emb), c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:k]]


_SCORE_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _normalize_judge_score(raw: Any) -> float:
    """Normalize judge score to 0-10 range."""
    val: float = 0.0
    if raw is None:
        val = 0.0
    elif isinstance(raw, (int, float)):
        val = float(raw)
    else:
        s = str(raw).strip()
        if s:
            m = _SCORE_RE.search(s)
            if m:
                try:
                    val = float(m.group(0))
                except Exception:
                    val = 0.0
    
    # If model used 0-100 scale, convert to 0-10
    if val > 10.0 and val <= 100.0:
        val = val / 10.0
    
    # Clamp to 0-10
    return max(0.0, min(10.0, val))


@dataclass(frozen=True)
class JudgeResult:
    passed: bool
    score: float
    reasoning: str
    details: Dict[str, Any]


# --- Judge prompts ---
P_JUDGE_ANSWER = """Ти оцінюєш відповідь українського бота техпідтримки.

ЗНАЙДЕНІ КЕЙСИ (база знань бота):
{kb_text}

ЗАПИТАННЯ КОРИСТУВАЧА:
{question}

ВІДПОВІДЬ БОТА:
{response}

ОЧІКУВАНА ПОВЕДІНКА: Бот повинен дати корисну відповідь на основі бази знань.
ОЧІКУВАНІ ТЕМИ: {expected_topics}

Оціни відповідь за критеріями:
1. ACCURACY (точність): немає вигаданих фактів поза базою знань
2. RELEVANCE (релевантність): відповідає на питання користувача
3. USEFULNESS (корисність): практичні кроки, як від компетентного агента
4. CONCISENESS (стислість): коротко і по суті
5. LANGUAGE (мова): правильна українська
6. FACTUAL (фактичність): перевір чи відповідь не суперечить даним з бази знань

Поверни ТІЛЬКИ JSON:
- passed: boolean (чи прийнятна відповідь)
- score: integer 0-10 (НЕ 0-100)
- accuracy_ok: boolean
- relevance_ok: boolean
- usefulness_ok: boolean
- concise_ok: boolean
- language_ok: boolean
- factual_ok: boolean (немає галюцинацій)
- reasoning: рядок (1-3 речення)
- issues: масив рядків (проблеми якщо є)
"""

P_JUDGE_SILENCE = """Ти оцінюєш поведінку бота техпідтримки.

ПОВІДОМЛЕННЯ:
sender: {sender}
body: {body}

ОЧІКУВАНА ПОВЕДІНКА: {expected_behavior}

БОТ: {bot_action}

Визнач чи бот правильно діяв (мовчав коли потрібно мовчати, або намагався відповісти коли не повинен був).

Поверни ТІЛЬКИ JSON:
- passed: boolean (чи правильна поведінка)
- score: integer 0-10 (10 = ідеально правильно, 0 = повністю невірно)
- reasoning: рядок (1-2 речення)
"""


class StreamingEvalJudge:
    """Gemini judge for streaming evaluation."""
    
    def __init__(self, model: str):
        if not os.environ.get("GOOGLE_API_KEY"):
            raise RuntimeError("GOOGLE_API_KEY missing")
        self.client = OpenAI(
            api_key=os.environ["GOOGLE_API_KEY"],
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
        self.model = model
    
    def evaluate_answer(
        self,
        *,
        question: str,
        response_text: str,
        evidence_cases: List[Dict[str, Any]],
        expected_topics: List[str],
    ) -> JudgeResult:
        """Evaluate a bot response that SHOULD have answered."""
        kb_text = "\n".join([
            f"Case {i+1}: {c.get('problem_title', '')}\n"
            f"Summary: {c.get('problem_summary', '')}\n"
            f"Solution: {c.get('solution_summary', '')}\n"
            f"Tags: {', '.join(c.get('tags') or [])}"
            for i, c in enumerate(evidence_cases)
        ]) if evidence_cases else "(no evidence)"
        
        prompt = P_JUDGE_ANSWER.format(
            kb_text=kb_text,
            question=question,
            response=response_text if response_text else "(БОТ НЕ ВІДПОВІВ)",
            expected_topics=", ".join(expected_topics) if expected_topics else "(не вказано)",
        )
        
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        
        score = _normalize_judge_score(data.get("score", 0))
        
        # If bot didn't respond when it should have, automatic fail
        if not response_text:
            score = 0.0
            data["passed"] = False
            data["reasoning"] = "Bot did not respond when it should have."
            data["issues"] = ["no_response"]
        
        return JudgeResult(
            passed=bool(data.get("passed", False)),
            score=score,
            reasoning=str(data.get("reasoning", "")),
            details={
                "score_raw": data.get("score"),
                "accuracy_ok": data.get("accuracy_ok"),
                "relevance_ok": data.get("relevance_ok"),
                "usefulness_ok": data.get("usefulness_ok"),
                "concise_ok": data.get("concise_ok"),
                "language_ok": data.get("language_ok"),
                "factual_ok": data.get("factual_ok"),
                "issues": data.get("issues", []),
            },
        )
    
    def evaluate_silence(
        self,
        *,
        sender: str,
        body: str,
        expected_behavior: str,
        bot_responded: bool,
        bot_response: Optional[str] = None,
    ) -> JudgeResult:
        """Evaluate whether bot correctly stayed silent (or incorrectly responded)."""
        if bot_responded:
            bot_action = f"Бот відповів: {bot_response}"
        else:
            bot_action = "Бот мовчав (не відповів)"
        
        prompt = P_JUDGE_SILENCE.format(
            sender=sender,
            body=body,
            expected_behavior=expected_behavior,
            bot_action=bot_action,
        )
        
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        
        score = _normalize_judge_score(data.get("score", 0))
        
        # Simple check: for "ignore"/"contains_answer", not responding is correct
        expected_silence = True
        actual_silence = not bot_responded
        correct_action = expected_silence == actual_silence
        
        if not correct_action:
            score = 0.0
            data["passed"] = False
        
        return JudgeResult(
            passed=bool(data.get("passed", correct_action)),
            score=score if correct_action else 0.0,
            reasoning=str(data.get("reasoning", "")),
            details={
                "expected_silence": expected_silence,
                "actual_silence": actual_silence,
                "correct_action": correct_action,
            },
        )


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    _maybe_load_dotenv(repo / ".env")
    
    if not os.environ.get("GOOGLE_API_KEY"):
        raise SystemExit("GOOGLE_API_KEY is not set.")
    
    # Fix embedding model: Gemini OpenAI endpoint requires gemini-embedding-001
    cur_emb = (os.environ.get("EMBEDDING_MODEL") or "").strip()
    if not cur_emb or cur_emb in {"text-embedding-004", "models/text-embedding-004"}:
        os.environ["EMBEDDING_MODEL"] = "gemini-embedding-001"
    
    # Paths
    data_dir = repo / "test" / "data" / "streaming_eval"
    kb_path = data_dir / "context_kb.json"
    eval_path = data_dir / "eval_messages_labeled.json"
    
    if not kb_path.exists():
        raise SystemExit(f"Missing KB: {kb_path}. Run: python test/prepare_streaming_eval_dataset.py")
    if not eval_path.exists():
        raise SystemExit(f"Missing eval data: {eval_path}. Run: python test/prepare_streaming_eval_dataset.py")
    
    # Load data
    kb_data = json.loads(kb_path.read_text(encoding="utf-8"))
    eval_data = json.loads(eval_path.read_text(encoding="utf-8"))
    
    kb_cases: List[Dict[str, Any]] = kb_data.get("cases") or []
    eval_messages: List[Dict[str, Any]] = eval_data.get("messages") or []
    group_name = kb_data.get("group_name", "")
    group_id = kb_data.get("group_id", "")
    
    print(f"Group: {group_name} ({group_id})")
    print(f"KB cases: {len(kb_cases)}")
    print(f"Eval messages: {len(eval_messages)}")
    print(f"Label distribution: {eval_data.get('label_counts', {})}")
    
    # Config
    top_k = int(os.environ.get("STREAMING_EVAL_TOP_K", "5"))
    judge_model = os.environ.get("JUDGE_MODEL", "gemini-2.5-flash-lite")
    
    # Initialize SupportBot LLM client
    sys.path.insert(0, str(repo / "signal-bot"))
    from app.config import load_settings
    from app.llm.client import LLMClient
    
    settings = load_settings()
    llm = LLMClient(settings)
    judge = StreamingEvalJudge(model=judge_model)
    
    # Context for decision/respond
    context = f"Група техпідтримки: {group_name}".strip()
    
    # Results
    results: List[Dict[str, Any]] = []
    
    # Build a rolling context window for more realistic evaluation
    context_window: List[str] = []
    context_window_size = 5
    
    # Build a simulated buffer from all context messages (simulating real buffer behavior)
    # Buffer format: "sender ts=timestamp\ncontent\n\n"
    buffer_lines: List[str] = []
    buffer_max_messages = 100  # Keep last N messages in buffer
    
    print("\n=== Running Evaluation ===")
    
    for msg in eval_messages:
        idx = msg["idx"]
        label = msg["label"]
        body = msg.get("body", "")
        sender = msg.get("sender", "unknown")
        timestamp = msg.get("timestamp", 0)
        expected_topics = msg.get("expected_topics", [])
        
        if not body.strip():
            continue
        
        # Add recent context
        rolling_context = "\n".join(context_window[-context_window_size:]) if context_window else ""
        full_context = f"{context}\n\nОстанні повідомлення:\n{rolling_context}" if rolling_context else context
        
        # Step 1: Run bot's decision
        consider = llm.decide_consider(message=body, context=full_context).consider
        
        # Step 2: If considering, retrieve KB and respond
        response_text: Optional[str] = None
        retrieved_cases: List[Dict[str, Any]] = []
        
        if consider:
            # Embed query and retrieve
            try:
                query_emb = llm.embed(text=body)
                retrieved_cases = _topk_cases(kb_cases, query_emb, k=top_k)
            except Exception as e:
                print(f"  Embedding error: {e}")
                retrieved_cases = []
            
            # Format cases for respond prompt (include status in metadata)
            cases_json = json.dumps([
                {
                    "case_id": f"kb-{c.get('idx')}",
                    "document": c.get("doc_text", ""),
                    "metadata": {
                        "group_id": group_id,
                        "status": c.get("status", "open"),
                    },
                    "distance": None,
                }
                for c in retrieved_cases
            ], ensure_ascii=False, indent=2)
            
            # Build buffer text from accumulated messages
            buffer_text = "\n\n".join(buffer_lines[-buffer_max_messages:])
            
            resp = llm.decide_and_respond(
                message=body, 
                context=full_context, 
                cases=cases_json,
                buffer=buffer_text,
            )
            if resp.respond:
                response_text = (resp.text or "").strip()
        
        # Step 3: Judge the result based on label
        if label == "answer":
            # Bot SHOULD have answered
            judged = judge.evaluate_answer(
                question=body,
                response_text=response_text or "",
                evidence_cases=retrieved_cases,
                expected_topics=expected_topics,
            )
        else:
            # Bot should NOT have answered ("ignore" or "contains_answer")
            expected_behavior = "Ігнорувати повідомлення" if label == "ignore" else "Не відповідати (вже є відповідь)"
            judged = judge.evaluate_silence(
                sender=sender,
                body=body,
                expected_behavior=expected_behavior,
                bot_responded=bool(response_text),
                bot_response=response_text,
            )
        
        result = {
            "idx": idx,
            "timestamp": timestamp,
            "sender": sender,
            "body": body[:200] + "..." if len(body) > 200 else body,  # Truncate for output
            "label": label,
            "expected_topics": expected_topics,
            "consider": consider,
            "responded": bool(response_text),
            "response_len": len(response_text or ""),
            "retrieved_cases_count": len(retrieved_cases),
            "judge_passed": judged.passed,
            "judge_score": judged.score,
            "judge_reasoning": judged.reasoning,
            "judge_details": judged.details,
        }
        results.append(result)
        
        # Update context window
        context_window.append(f"{sender}: {body[:100]}")
        
        # Update buffer (simulating real buffer accumulation)
        buffer_lines.append(f"{sender} ts={timestamp}\n{body}")
        
        # Progress
        status = "PASS" if judged.passed else "FAIL"
        print(f"  [{idx:3d}] {label:16s} | consider={consider} resp={bool(response_text)} | {status} score={judged.score:.1f}")
    
    # =====================
    # Compute Summary
    # =====================
    def _summarize(cat: str) -> Dict[str, Any]:
        xs = [r for r in results if r["label"] == cat]
        if not xs:
            return {"n": 0}
        passed = sum(1 for r in xs if r["judge_passed"])
        avg_score = sum(r["judge_score"] for r in xs) / len(xs)
        responded = sum(1 for r in xs if r["responded"])
        return {
            "n": len(xs),
            "passed": passed,
            "pass_rate": round(passed / len(xs), 3),
            "avg_score": round(avg_score, 2),
            "responded_count": responded,
            "respond_rate": round(responded / len(xs), 3) if xs else 0,
        }
    
    summary = {
        "group_name": group_name,
        "group_id": group_id,
        "top_k": top_k,
        "kb_case_count": len(kb_cases),
        "eval_message_count": len(eval_messages),
        "results_count": len(results),
        "evaluated_at": _now_tag(),
        "by_label": {
            "answer": _summarize("answer"),
            "ignore": _summarize("ignore"),
            "contains_answer": _summarize("contains_answer"),
        },
        "overall": {
            "total": len(results),
            "passed": sum(1 for r in results if r["judge_passed"]),
            "pass_rate": round(sum(1 for r in results if r["judge_passed"]) / len(results), 3) if results else 0,
            "avg_score": round(sum(r["judge_score"] for r in results) / len(results), 2) if results else 0,
        },
    }
    
    # Save results
    results_path = data_dir / "eval_results.json"
    results_path.write_text(json.dumps({
        "summary": summary,
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved results: {results_path}")
    
    summary_path = data_dir / "eval_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved summary: {summary_path}")
    
    # Print summary
    print("\n=== Evaluation Summary ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
