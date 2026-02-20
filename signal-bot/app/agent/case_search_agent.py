"""CaseSearchAgent — multi-buffer context retrieval.

Answer pipeline:
1. SCRAG  — semantic search in ChromaDB (solved cases, permanent)
2. B3     — recently solved cases still within the rolling B2 window (DB query)
3. B1     — currently open/unresolved cases for this group (DB query)

Response logic:
- SCRAG hit with solution  → synthesize direct answer + link
- B3 hit                   → synthesize from recent context
- Only B1 hit              → TAG_ADMIN with context ("case being tracked")
- Nothing                  → TAG_ADMIN
"""
from __future__ import annotations

import logging
import sys
import time
from typing import Any, Dict, List, Optional

sys.stdout.reconfigure(encoding="utf-8")

log = logging.getLogger(__name__)


SCRAG_TOP_K = 3  # always return top-K results; let the synthesizer decide relevance


class CaseSearchAgent:
    def __init__(self, rag=None, llm=None, public_url: str = "https://supportbot.info"):
        self.rag = rag
        self.llm = llm
        self.public_url = public_url

    # ─── SCRAG search ────────────────────────────────────────────────────────

    def _search_scrag(self, query: str, group_id: str, k: int = 3) -> List[Dict[str, Any]]:
        """Semantic search in ChromaDB (solved cases only).

        Only returns results with cosine distance <= SCRAG_DISTANCE_THRESHOLD so
        completely unrelated cases are never injected into the synthesizer context.
        """
        if not self.rag or not self.llm:
            return []
        try:
            query_emb = self.llm.embed(text=query)
            results = self.rag.retrieve_cases(group_id=group_id, embedding=query_emb, k=k)
            formatted = []
            for r in results:
                distance = r.get("distance") or 1.0
                log.debug(
                    "SCRAG case %s distance=%.3f",
                    r.get("case_id", "")[:16],
                    distance,
                )
                doc = r.get("document", "")
                # Parse the structured doc format: [SOLVED] title\nПроблема: ...\nРішення: ...
                problem, solution = "", ""
                for line in doc.splitlines():
                    if line.startswith("Проблема:"):
                        problem = line.replace("Проблема:", "").strip()
                    elif line.startswith("Рішення:"):
                        solution = line.replace("Рішення:", "").strip()
                if not problem:
                    lines = [l for l in doc.splitlines() if l.strip()]
                    problem = lines[1] if len(lines) > 1 else doc[:100]
                if not solution:
                    lines = [l for l in doc.splitlines() if l.strip()]
                    solution = lines[2] if len(lines) > 2 else ""
                if not solution:
                    log.debug("SCRAG case %s has no solution, skipping", r.get("case_id"))
                    continue
                formatted.append({
                    "source": "scrag",
                    "case_id": r["case_id"],
                    "score": 1.0 - distance,
                    "problem": problem,
                    "solution": solution,
                    "doc_text": doc,
                })
            return formatted
        except Exception:
            log.exception("SCRAG search failed")
            return []

    # ─── B3 context ──────────────────────────────────────────────────────────

    def _get_b3_context(self, group_id: str, db) -> List[Dict[str, Any]]:
        """Return recently solved cases still in the B2 rolling window (B3)."""
        if db is None:
            return []
        try:
            from app.db import get_buffer, get_recent_solved_cases
            buf = get_buffer(db, group_id=group_id) or ""
            if not buf.strip():
                return []
            # Find the oldest timestamp in the buffer
            import re
            ts_matches = re.findall(r"\bts=(\d+)\b", buf)
            since_ts = min(int(t) for t in ts_matches) if ts_matches else 0
            if since_ts == 0:
                return []
            cases = get_recent_solved_cases(db, group_id=group_id, since_ts_ms=since_ts)
            return [
                {
                    "source": "b3",
                    "case_id": c["case_id"],
                    "problem": c["problem_title"],
                    "solution": c["solution_summary"],
                }
                for c in cases
                if c.get("solution_summary", "").strip()
            ]
        except Exception:
            log.exception("B3 context fetch failed")
            return []

    # ─── B1 context ──────────────────────────────────────────────────────────

    def _get_b1_context(self, group_id: str, db) -> List[Dict[str, Any]]:
        """Return currently open/unresolved cases for this group (B1)."""
        if db is None:
            return []
        try:
            from app.db import get_open_cases_for_group
            cases = get_open_cases_for_group(db, group_id=group_id)
            return [
                {
                    "source": "b1",
                    "case_id": c["case_id"],
                    "problem": c["problem_title"],
                    "problem_summary": c["problem_summary"],
                }
                for c in cases
            ]
        except Exception:
            log.exception("B1 context fetch failed")
            return []

    # ─── Public API ──────────────────────────────────────────────────────────

    def search(self, query: str, group_id: Optional[str] = None, db=None, k: int = 3) -> Dict[str, Any]:
        """Return all context layers for this query.

        Returns a dict with keys: scrag, b3, b1.
        """
        if not group_id:
            log.info("CaseSearchAgent: no group_id, skipping search for security")
            return {"scrag": [], "b3": [], "b1": []}

        log.info("CaseSearchAgent: searching for '%s' (group=%s)", query[:60], group_id[:20])
        scrag = self._search_scrag(query, group_id=group_id, k=k)
        b3 = self._get_b3_context(group_id=group_id, db=db)
        b1 = self._get_b1_context(group_id=group_id, db=db)
        log.info(
            "CaseSearchAgent results: scrag=%d b3=%d b1=%d",
            len(scrag), len(b3), len(b1),
        )
        return {"scrag": scrag, "b3": b3, "b1": b1}

    def _case_exists_in_db(self, case_id: str, db) -> bool:
        """Check that a case_id still exists in MySQL as a non-archived case.

        Returns False for archived cases so the bot never cites them in new answers
        (archived cases stay in MySQL for old-link preservation but leave ChromaDB).
        """
        if db is None:
            return True  # Can't validate without DB — assume OK
        try:
            from app.db import get_case
            case = get_case(db, case_id)
            if case is None:
                return False
            return case.get("status") != "archived"
        except Exception:
            return True  # On error, be permissive

    def answer(self, question: str, group_id: Optional[str] = None, db=None) -> str:
        """Return a formatted context string for the synthesizer, or signal tags.

        Returns:
          - "No relevant cases found."  → synthesizer will TAG_ADMIN
          - "B1_ONLY:<context>"         → synthesizer should mention tracked case + TAG_ADMIN
          - Formatted solved context    → synthesizer should answer directly
        """
        ctx = self.search(question, group_id=group_id, db=db)
        scrag = ctx["scrag"]
        b3 = ctx["b3"]
        b1 = ctx["b1"]

        # Build response text from solved context (SCRAG + B3 merged, deduplicated).
        # Validate each case_id against MySQL to filter out stale ChromaDB entries
        # that were not cleaned up during a previous re-ingest.
        solved: List[Dict[str, Any]] = []
        seen_ids: set = set()
        for item in scrag:
            cid = item.get("case_id", "")
            if cid in seen_ids:
                continue
            if not self._case_exists_in_db(cid, db):
                log.warning("SCRAG case %s not found in MySQL — skipping stale entry", cid[:16])
                # Best-effort cleanup so this won't pollute future results
                if self.rag:
                    try:
                        self.rag.delete_cases([cid])
                    except Exception:
                        pass
                continue
            seen_ids.add(cid)
            solved.append(item)
        for item in b3:
            cid = item.get("case_id", "")
            if cid not in seen_ids:
                seen_ids.add(cid)
                solved.append(item)

        if solved:
            text = "Found similar past cases:\n"
            for r in solved:
                score_str = f" (Score: {r['score']:.2f})" if "score" in r else ""
                text += f"- {score_str}:\n"
                text += f"  Problem: {r['problem']}\n"
                text += f"  Solution: {r['solution']}\n"
                link = f"{self.public_url}/case/{r['case_id']}"
                text += f"  Link: [{link}]\n"
            return text

        # No solved cases found — check B1
        if b1:
            b1_text = "OPEN_CASES:\n"
            for c in b1[:3]:  # cap to avoid overly long context
                link = f"{self.public_url}/case/{c['case_id']}"
                b1_text += f"- [OPEN] {c['problem']}: {c.get('problem_summary', '')[:120]}\n"
                b1_text += f"  Link: {link}\n"
            return f"B1_ONLY:{b1_text}"

        return "No relevant cases found."
