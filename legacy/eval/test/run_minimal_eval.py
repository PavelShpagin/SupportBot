#!/usr/bin/env python3
"""
Minimal real case evaluation with careful quota management.

Strategy:
1. Reuse cached embeddings where possible
2. Only evaluate a few cases at a time
3. Save intermediate results
4. Use cheaper models where possible
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).parent.parent / "signal-bot"))

from app.llm.client import LLMClient
from app.settings import Settings


def load_cases(cases_path: Path) -> List[Dict[str, Any]]:
    """Load structured cases."""
    data = json.loads(cases_path.read_text(encoding="utf-8"))
    return data.get("cases", [])


def _extract_first_user_text(case_block: str) -> str:
    """Extract first message text from case block."""
    import re
    lines = [ln.rstrip() for ln in (case_block or "").splitlines()]
    buf: List[str] = []
    for ln in lines:
        if re.match(r"^.+\s+ts=\d+", ln.strip()):
            if buf:
                break
            continue
        if ln.strip():
            buf.append(ln.strip())
            if len(" ".join(buf)) > 220:
                break
    q = " ".join(buf).strip()
    return q[:280]


def evaluate_single_case(
    llm: LLMClient,
    case: Dict[str, Any],
    all_cases: List[Dict[str, Any]],
    context: str,
    k: int = 3,
) -> Dict[str, Any]:
    """Evaluate a single case. Returns results dict."""
    
    # Extract question from case block
    case_block = case.get("case_block", "")
    question = _extract_first_user_text(case_block)
    
    if not question:
        return {
            "error": "No question extracted",
            "case_idx": case.get("idx"),
        }
    
    # Retrieve similar cases (using pre-computed embeddings)
    query_emb = case.get("embedding", [])
    if not query_emb:
        return {
            "error": "No embedding found for case",
            "case_idx": case.get("idx"),
        }
    
    # Calculate similarity with other cases
    def dot(a, b):
        return sum(x * y for x, y in zip(a, b))
    
    scored = []
    for other in all_cases:
        if other.get("idx") == case.get("idx"):
            continue  # Don't retrieve self
        other_emb = other.get("embedding", [])
        if not other_emb:
            continue
        score = dot(query_emb, other_emb)
        scored.append((score, other))
    
    scored.sort(key=lambda x: x[0], reverse=True)
    top_cases = [c for _, c in scored[:k]]
    
    # Stage 1: Consider
    consider_result = llm.decide_consider(message=question, context=context)
    
    if not consider_result.consider:
        return {
            "case_idx": case.get("idx"),
            "case_title": case.get("problem_title", "")[:60],
            "question": question[:100],
            "consider": False,
            "respond": False,
            "response": None,
            "note": "Filtered by Stage 1",
        }
    
    # Stage 2: Respond
    retrieved = [
        {
            "case_id": f"real-{c.get('idx')}",
            "document": c.get("doc_text", ""),
            "metadata": {"status": c.get("status", "")},
            "distance": None,
        }
        for c in top_cases
    ]
    
    cases_json = json.dumps(retrieved, ensure_ascii=False, indent=2)
    respond_result = llm.decide_and_respond(
        message=question,
        context=context,
        cases=cases_json,
    )
    
    return {
        "case_idx": case.get("idx"),
        "case_title": case.get("problem_title", "")[:60],
        "case_status": case.get("status"),
        "question": question[:100],
        "consider": True,
        "respond": respond_result.respond,
        "response": (respond_result.text or "").strip() if respond_result.respond else None,
        "response_len": len(respond_result.text or "") if respond_result.respond else 0,
        "citations": respond_result.citations if respond_result.respond else [],
        "retrieved_cases": [c.get("idx") for c in top_cases],
    }


def main():
    # Load environment
    repo = Path(__file__).parent.parent
    dotenv_path = repo / ".env"
    
    if dotenv_path.exists():
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
    
    if not os.environ.get("GOOGLE_API_KEY"):
        print("❌ GOOGLE_API_KEY not set")
        sys.exit(1)
    
    # Load cases
    cases_path = repo / "test" / "data" / "signal_cases_structured.json"
    if not cases_path.exists():
        print(f"❌ Cases file not found: {cases_path}")
        sys.exit(1)
    
    data = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = data.get("cases", [])
    group_name = data.get("group_name", "")
    
    print(f"📊 Loaded {len(cases)} cases")
    print(f"Group: {group_name}\n")
    
    # Check for how many to evaluate
    max_eval = int(os.environ.get("MAX_EVAL_CASES", "3"))
    cases_to_eval = cases[:max_eval]
    
    print(f"⚠️  Quota protection: evaluating only {len(cases_to_eval)}/{len(cases)} cases")
    print(f"   Set MAX_EVAL_CASES=N to change\n")
    
    # Initialize LLM (reuse embeddings, so no embed calls needed)
    settings = Settings()
    llm = LLMClient(settings=settings)
    
    context = f"Група техпідтримки: {group_name}"
    
    # Evaluate cases one by one
    results = []
    
    for i, case in enumerate(cases_to_eval, 1):
        print(f"[{i}/{len(cases_to_eval)}] Evaluating case #{case.get('idx')}...", flush=True)
        
        try:
            result = evaluate_single_case(llm, case, cases, context, k=3)
            results.append(result)
            
            # Print result
            if result.get("error"):
                print(f"  ❌ Error: {result['error']}")
            elif not result.get("consider"):
                print(f"  ⏭️  Skipped (Stage 1 filtered)")
            elif not result.get("respond"):
                print(f"  🚫 No response (Stage 2 declined)")
            else:
                print(f"  ✅ Responded ({result['response_len']} chars)")
                print(f"     Title: {result['case_title']}")
            
        except Exception as e:
            print(f"  ❌ Exception: {e}")
            if "rate" in str(e).lower() or "quota" in str(e).lower():
                print("\n⚠️  API quota/rate limit hit! Stopping early.")
                print(f"   Completed {i-1}/{len(cases_to_eval)} cases")
                break
            results.append({
                "case_idx": case.get("idx"),
                "error": str(e),
            })
    
    # Summary
    print("\n" + "="*80)
    print("📊 RESULTS SUMMARY")
    print("="*80)
    
    successful = [r for r in results if not r.get("error")]
    considered = [r for r in successful if r.get("consider")]
    responded = [r for r in considered if r.get("respond")]
    
    print(f"  Total evaluated: {len(results)}")
    print(f"  Successful: {len(successful)}")
    print(f"  Considered (Stage 1): {len(considered)} ({100*len(considered)/max(1,len(successful)):.1f}%)")
    print(f"  Responded (Stage 2): {len(responded)} ({100*len(responded)/max(1,len(considered)):.1f}%)")
    
    if responded:
        avg_len = sum(r["response_len"] for r in responded) / len(responded)
        print(f"  Avg response length: {avg_len:.0f} chars")
    
    # Save results
    out_path = repo / "test" / "data" / "minimal_eval_results.json"
    out_data = {
        "timestamp": str(Path(__file__).stat().st_mtime),
        "n_evaluated": len(results),
        "n_successful": len(successful),
        "n_considered": len(considered),
        "n_responded": len(responded),
        "results": results,
    }
    out_path.write_text(json.dumps(out_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n💾 Saved results to: {out_path}")


if __name__ == "__main__":
    main()
