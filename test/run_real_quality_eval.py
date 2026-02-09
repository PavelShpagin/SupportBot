#!/usr/bin/env python3
"""
Real-data quality evaluation for SupportBot.

Uses:
- Structured solved cases mined from Signal history (test/data/signal_cases_structured.json)
- SupportBot's real decision + respond prompts
- Gemini-as-judge scoring

Outputs (gitignored):
- test/data/real_quality_eval.json

Usage:
  source .venv/bin/activate
  python test/run_real_quality_eval.py

Notes:
- This script loads `.env` itself (CRLF-safe), so you don't need to `source .env`.

Optional env vars:
- REAL_OUT_DIR: write outputs into this directory (default: test/data/)
- REAL_CASES_PATH: override structured cases JSON (default: <REAL_OUT_DIR>/signal_cases_structured.json)
"""

from __future__ import annotations

import json
import os
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _maybe_load_dotenv(dotenv_path: Path) -> None:
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


def _extract_first_user_text(case_block: str) -> str:
    """
    case_block format is like:
      sender ts=...\ntext\n\nsender2 ts=...\ntext2\n...
    We take the first non-empty message text line after a header.
    """
    lines = [ln.rstrip() for ln in (case_block or "").splitlines()]
    buf: List[str] = []
    for ln in lines:
        if re.match(r"^.+\\sts=\\d+", ln.strip()):
            # header line; next non-empty line(s) form the message
            if buf:
                break
            continue
        if ln.strip():
            buf.append(ln.strip())
            # stop early; we only need a short question
            if len(" ".join(buf)) > 220:
                break
    q = " ".join(buf).strip()
    return q[:280]


def _dot(a: List[float], b: List[float]) -> float:
    return float(sum(x * y for x, y in zip(a, b)))


def _topk_cases(cases: List[Dict[str, Any]], query_emb: List[float], k: int) -> List[Dict[str, Any]]:
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
    """
    The judge is instructed to return a numeric score in [0, 10].

    In practice, some models occasionally return:
    - 0-100 scale (e.g. 86)
    - strings like "8/10"

    Normalize into a float clamped to [0, 10].
    """
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

    # If the model used a 0-100 scale, convert to 0-10.
    if val > 10.0 and val <= 100.0:
        val = val / 10.0

    # Clamp to 0-10.
    if val < 0.0:
        val = 0.0
    elif val > 10.0:
        val = 10.0

    return float(val)


@dataclass(frozen=True)
class JudgeResult:
    passed: bool
    score: float
    reasoning: str
    details: Dict[str, Any]


class GeminiJudge:
    def __init__(self):
        from openai import OpenAI

        if not os.environ.get("GOOGLE_API_KEY"):
            raise RuntimeError("GOOGLE_API_KEY missing (load .env or export it).")
        self.client = OpenAI(
            api_key=os.environ["GOOGLE_API_KEY"],
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
        self.model = os.environ.get("JUDGE_MODEL", "gemini-2.5-flash-lite")

    def evaluate(
        self,
        *,
        question: str,
        response_text: str,
        expected_behavior: str,
        evidence_cases: List[Dict[str, Any]],
        category: str,
    ) -> JudgeResult:
        kb_text = "\n".join(
            [
                f"Case {i+1}: {c.get('problem_title','')}\n"
                f"Summary: {c.get('problem_summary','')}\n"
                f"Solution: {c.get('solution_summary','')}\n"
                f"Tags: {', '.join(c.get('tags') or [])}"
                for i, c in enumerate(evidence_cases)
            ]
        )
        prompt = f"""You are evaluating a Ukrainian tech support bot's response.

EVIDENCE CASES (what the bot is allowed to use):
{kb_text if kb_text else "(no evidence)"}

USER QUESTION: {question}

BOT RESPONSE: {response_text if response_text else "(NO RESPONSE - bot stayed silent)"}

EXPECTED BEHAVIOR: {expected_behavior}
CATEGORY: {category}

Evaluate the response on:
1. ACCURACY: no made-up facts outside EVIDENCE CASES
2. RELEVANCE: addresses the user's question
3. USEFULNESS: actionable steps, like a competent human agent
4. CONCISENESS: brief and to the point
5. LANGUAGE: proper Ukrainian
6. APPROPRIATE_ACTION:
   - should_answer: give a helpful answer based on evidence
   - should_decline: do NOT answer substantively (silence or ask for clarification only)
   - should_ignore: stay silent

Return JSON with:
- passed: boolean
- score: integer from 0 to 10 (NOT 0-100)
- accuracy_ok: boolean
- relevance_ok: boolean
- usefulness_ok: boolean
- concise_ok: boolean
- language_ok: boolean
- action_ok: boolean
- reasoning: string (1-3 sentences)
- issues: array of strings
"""
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        raw_score = data.get("score", 0)
        score = _normalize_judge_score(raw_score)
        return JudgeResult(
            passed=bool(data.get("passed", False)),
            score=score,
            reasoning=str(data.get("reasoning", "")),
            details={
                "score_raw": raw_score,
                "accuracy_ok": data.get("accuracy_ok"),
                "relevance_ok": data.get("relevance_ok"),
                "usefulness_ok": data.get("usefulness_ok"),
                "concise_ok": data.get("concise_ok"),
                "language_ok": data.get("language_ok"),
                "action_ok": data.get("action_ok"),
                "issues": data.get("issues", []),
            },
        )


def main() -> None:
    repo = Path(__file__).parent.parent
    _maybe_load_dotenv(repo / ".env")

    out_dir_raw = (os.environ.get("REAL_OUT_DIR") or "").strip()
    out_dir = Path(out_dir_raw) if out_dir_raw else (repo / "test" / "data")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Embedding model: Gemini OpenAI endpoint supports gemini-embedding-001.
    # Many older configs use text-embedding-004 which is often NOT available via the OpenAI-compatible Gemini endpoint.
    cur_emb = (os.environ.get("EMBEDDING_MODEL") or "").strip()
    if not cur_emb or cur_emb in {"text-embedding-004", "models/text-embedding-004"}:
        os.environ["EMBEDDING_MODEL"] = "gemini-embedding-001"

    cases_path_raw = (os.environ.get("REAL_CASES_PATH") or "").strip()
    cases_path = Path(cases_path_raw) if cases_path_raw else (out_dir / "signal_cases_structured.json")
    if not cases_path.exists():
        raise SystemExit(f"Missing cases: {cases_path}. Run: python test/mine_real_cases.py")

    data = json.loads(cases_path.read_text(encoding="utf-8"))
    group_name = data.get("group_name") or ""
    group_id = data.get("group_id") or ""
    cases: List[Dict[str, Any]] = data.get("cases") or []
    if not cases:
        raise SystemExit("No structured cases to evaluate.")

    # Load SupportBot LLM client
    import sys as _sys

    _sys.path.insert(0, str(repo / "signal-bot"))
    from app.config import load_settings  # noqa: E402
    from app.llm.client import LLMClient  # noqa: E402

    settings = load_settings()
    llm = LLMClient(settings)

    judge = GeminiJudge()

    # Build scenarios
    random.seed(0)
    k = int(os.environ.get("REAL_EVAL_TOP_K", "5"))
    n_should_answer = int(os.environ.get("REAL_EVAL_N", "18"))
    sampled_cases = cases[:]
    random.shuffle(sampled_cases)
    sampled_cases = sampled_cases[: min(n_should_answer, len(sampled_cases))]

    scenarios: List[Dict[str, Any]] = []
    for i, c in enumerate(sampled_cases, 1):
        q = _extract_first_user_text(c.get("case_block", "")) or (c.get("problem_title") or "")
        scenarios.append(
            {
                "id": f"case_{i:02d}",
                "category": "should_answer",
                "question": q,
                "expected_behavior": "Answer like a competent human support agent using the evidence.",
                "case_hint": {
                    "problem_title": c.get("problem_title"),
                    "solution_summary": c.get("solution_summary"),
                },
            }
        )

    # Add decline/ignore scenarios
    scenarios.extend(
        [
            {
                "id": "decline_kubernetes",
                "category": "should_decline",
                "question": "Як налаштувати Kubernetes кластер для продакшену?",
                "expected_behavior": "Do not answer substantively (unrelated).",
            },
            {
                "id": "decline_restaurant",
                "category": "should_decline",
                "question": "Порекомендуй хороший ресторан у Києві",
                "expected_behavior": "Do not answer (unrelated).",
            },
            {
                "id": "ignore_greeting",
                "category": "should_ignore",
                "question": "Привіт всім!",
                "expected_behavior": "Ignore greeting.",
            },
            {
                "id": "ignore_emoji",
                "category": "should_ignore",
                "question": "👍",
                "expected_behavior": "Ignore emoji-only.",
            },
        ]
    )

    # Run evaluation
    results: List[Dict[str, Any]] = []
    context = f"Група техпідтримки: {group_name}".strip()

    for s in scenarios:
        q = s["question"]

        consider = llm.decide_consider(message=q, context=context).consider

        response_text: Optional[str] = None
        retrieved_docs: List[Dict[str, Any]] = []

        if consider and s["category"] != "should_ignore":
            q_emb = llm.embed(text=q)
            top = _topk_cases(cases, q_emb, k=k)
            retrieved_docs = top
            retrieved = [
                {
                    "case_id": f"real-{c.get('idx')}",
                    "document": c.get("doc_text", ""),
                    "metadata": {"group_id": group_id, "status": c.get("status", "")},
                    "distance": None,
                }
                for c in top
            ]
            cases_json = json.dumps(retrieved, ensure_ascii=False, indent=2)
            resp = llm.decide_and_respond(message=q, context=context, cases=cases_json)
            if resp.respond:
                response_text = (resp.text or "").strip()

        # Expected behavior for ignore: consider should ideally be False and response None
        judged = judge.evaluate(
            question=q,
            response_text=response_text or "",
            expected_behavior=s["expected_behavior"],
            evidence_cases=retrieved_docs,
            category=s["category"],
        )

        results.append(
            {
                "id": s["id"],
                "category": s["category"],
                "consider": consider,
                "responded": bool(response_text),
                "response_len": len(response_text or ""),
                "judge_passed": judged.passed,
                "judge_score": judged.score,
                "judge_reasoning": judged.reasoning,
                "judge_details": judged.details,
                # Keep question/response for report generation (redaction happens in results.md)
                "question": q,
                "response": response_text or "",
                "case_hint": s.get("case_hint"),
            }
        )
        print(f"{s['id']}: consider={consider} responded={bool(response_text)} score={judged.score} pass={judged.passed}")

    # Summaries
    def _summ(cat: str) -> Dict[str, Any]:
        xs = [r for r in results if r["category"] == cat]
        if not xs:
            return {"n": 0}
        passed = sum(1 for r in xs if r["judge_passed"])
        avg = sum(float(r["judge_score"]) for r in xs) / len(xs)
        avg_len = sum(int(r["response_len"]) for r in xs) / max(1, sum(1 for r in xs if r["responded"]))
        return {"n": len(xs), "passed": passed, "pass_rate": passed / len(xs), "avg_score": avg, "avg_len_if_responded": avg_len}

    summary = {
        "group_name": group_name,
        "group_id": group_id,
        "k": k,
        "n_cases_total": len(cases),
        "n_scenarios": len(scenarios),
        "by_category": {
            "should_answer": _summ("should_answer"),
            "should_decline": _summ("should_decline"),
            "should_ignore": _summ("should_ignore"),
        },
    }

    out = out_dir / "real_quality_eval.json"
    out.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote: {out}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

