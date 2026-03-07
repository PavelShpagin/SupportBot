"""CaseSearchAgent — multi-buffer context retrieval.

Answer pipeline:
1. SCRAG   — semantic search in ChromaDB (solved cases, highest trust)
2. RCRAG   — semantic search in ChromaDB (recommendation cases, unconfirmed advice)
3. B3      — recently solved cases still within the rolling B2 window (DB query)
4. RCRAG-DB — recommendation cases for this group not yet in RAG (DB query)

Response logic:
- SCRAG hit with solution  → synthesize direct answer + link
- RCRAG hit                → synthesize answer with caveat ("not confirmed")
- B3 hit                   → synthesize from recent context
- Only RCRAG-DB hit        → TAG_ADMIN with recommendation context
- Nothing                  → TAG_ADMIN
"""
from __future__ import annotations

import logging
import sys
import time
from typing import Any, Dict, List, Optional

sys.stdout.reconfigure(encoding="utf-8")

log = logging.getLogger(__name__)


SCRAG_TOP_K = 3
# Cosine distance threshold: cases with distance > this are too dissimilar to use.
# Lower = stricter. 0.75 keeps good matches while dropping unrelated queries (0.9+).
SCRAG_DISTANCE_THRESHOLD = 0.75


class CaseSearchAgent:
    def __init__(self, rag=None, llm=None, public_url: str = "https://supportbot.info"):
        self.rag = rag
        self.llm = llm
        self.public_url = public_url
        self.last_search_counts: Dict[str, int] = {"solved": 0, "recommendation": 0}

    # ─── SCRAG search ────────────────────────────────────────────────────────

    def _search_scrag(self, query: str, group_id: str, k: int = 3, db=None) -> List[Dict[str, Any]]:
        """Search SCRAG (solved) and RCRAG (recommendation) as separate collections.

        Queries both collections independently and merges results with solved first.
        Only returns results with cosine distance <= SCRAG_DISTANCE_THRESHOLD.
        Resolves union group_ids so all groups in a union are searched together.
        """
        if not self.rag or not self.llm:
            return []
        try:
            # Resolve union: search across all groups in the same union
            union_gids = None
            if db is not None:
                try:
                    from app.db import get_union_group_ids
                    union_gids = get_union_group_ids(db, group_id)
                    if len(union_gids) <= 1:
                        union_gids = None  # No union, use default single-group search
                except Exception:
                    pass
            query_emb = self.llm.embed(text=query)
            # Query each collection independently for proper ranking
            scrag_results = self.rag.scrag.retrieve_cases(group_id=group_id, group_ids=union_gids, embedding=query_emb, k=k, status=None)
            rcrag_results = self.rag.rcrag.retrieve_cases(group_id=group_id, group_ids=union_gids, embedding=query_emb, k=k, status=None)

            formatted = []
            for source_tag, results in [("scrag", scrag_results), ("rcrag", rcrag_results)]:
                for r in results:
                    distance = r.get("distance") or 1.0
                    if distance > SCRAG_DISTANCE_THRESHOLD:
                        continue
                    doc = r.get("document", "")
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
                        continue
                    formatted.append({
                        "source": source_tag,
                        "status": "solved" if source_tag == "scrag" else "recommendation",
                        "case_id": r["case_id"],
                        "score": 1.0 - distance,
                        "problem": problem,
                        "solution": solution,
                        "doc_text": doc,
                    })
            # Deduplicate by case_id (promotion may have left stale RCRAG entry)
            seen = set()
            deduped = []
            for item in formatted:
                if item["case_id"] not in seen:
                    seen.add(item["case_id"])
                    deduped.append(item)
            # Sort: solved first (higher trust), then by score within each tier
            deduped.sort(key=lambda x: (0 if x["status"] == "solved" else 1, -x["score"]))
            return deduped
        except Exception:
            log.exception("SCRAG/RCRAG search failed")
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

    # ─── RCRAG-DB context (recommendation cases not yet in RAG) ─────────────

    def _get_b1_context(self, group_id: str, db) -> List[Dict[str, Any]]:
        """Return recommendation cases for this group (unconfirmed advice, not yet in ChromaDB)."""
        if db is None:
            return []
        try:
            from app.db import get_recommendation_cases_for_group
            cases = get_recommendation_cases_for_group(db, group_id=group_id)
            return [
                {
                    "source": "b1",
                    "case_id": c["case_id"],
                    "problem": c["problem_title"],
                    "problem_summary": c["problem_summary"],
                    "solution": c.get("solution_summary", ""),
                    "status": "recommendation",
                }
                for c in cases
            ]
        except Exception:
            log.exception("RCRAG-DB context fetch failed")
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
        scrag = self._search_scrag(query, group_id=group_id, k=k, db=db)
        b3 = self._get_b3_context(group_id=group_id, db=db)
        b1 = self._get_b1_context(group_id=group_id, db=db)
        log.info(
            "CaseSearchAgent results: scrag=%d b3=%d b1=%d",
            len(scrag), len(b3), len(b1),
        )
        return {"scrag": scrag, "b3": b3, "b1": b1}

    def answer(self, question: str, group_id: Optional[str] = None, db=None) -> str:
        """Return a formatted context string for the synthesizer, or signal tags.

        Returns:
          - "No relevant cases found."  → synthesizer will TAG_ADMIN
          - "B1_ONLY:<context>"         → synthesizer should mention recommendation case + TAG_ADMIN
          - Formatted solved context    → synthesizer should answer directly
        """
        ctx = self.search(question, group_id=group_id, db=db)
        scrag = ctx["scrag"]
        b3 = ctx["b3"]
        b1 = ctx["b1"]

        # Build response text from solved context (SCRAG + B3 merged, deduplicated).
        # Chroma is kept in sync with MySQL by the periodic SYNC_RAG worker job,
        # so no per-query MySQL round-trip is needed here.
        solved: List[Dict[str, Any]] = []
        seen_ids: set = set()
        for item in scrag:
            cid = item.get("case_id", "")
            if cid not in seen_ids:
                seen_ids.add(cid)
                solved.append(item)
        for item in b3:
            cid = item.get("case_id", "")
            if cid not in seen_ids:
                seen_ids.add(cid)
                solved.append(item)

        # Track counts for the response footer
        n_solved = sum(1 for s in solved if s.get("status") != "recommendation")
        n_reco = sum(1 for s in solved if s.get("status") == "recommendation") + len(b1)
        self.last_search_counts = {"solved": n_solved, "recommendation": n_reco}

        if solved:
            text = "Found similar past cases:\n"
            for r in solved:
                score_str = f" (Score: {r['score']:.2f})" if "score" in r else ""
                status_prefix = ""
                if r.get("status") == "recommendation":
                    status_prefix = "[Рекомендація — не підтверджено] "
                text += f"- {status_prefix}{score_str}:\n"
                text += f"  Problem: {r['problem']}\n"
                text += f"  Solution: {r['solution']}\n"
                link = f"{self.public_url}/case/{r['case_id']}"
                text += f"  Link: [{link}]\n"
            return text

        # No solved/recommendation from RAG — check RCRAG-DB (recommendation cases not yet in RAG)
        if b1:
            self.last_search_counts = {"solved": 0, "recommendation": len(b1)}
            b1_text = "RECOMMENDATION_CASES:\n"
            for c in b1[:3]:  # cap to avoid overly long context
                link = f"{self.public_url}/case/{c['case_id']}"
                b1_text += f"- [Рекомендація — не підтверджено] {c['problem']}: {c.get('solution', c.get('problem_summary', ''))[:120]}\n"
                b1_text += f"  Link: {link}\n"
            return f"B1_ONLY:{b1_text}"

        return "No relevant cases found."

    def get_evidence_files(self, case_answer: str, db=None) -> list[str]:
        """Extract non-image evidence file URLs from cases referenced in the answer.

        Parses case IDs from the answer text and looks up their evidence
        attachment URLs, filtering out images to return only files (pdf, zip, etc.).
        """
        if db is None or "No relevant cases" in case_answer:
            return []

        IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.heic', '.svg'}

        import re
        case_ids = re.findall(r'/case/([a-zA-Z0-9_-]+)', case_answer)
        if not case_ids:
            return []

        files: list[str] = []
        try:
            from app.db import get_case_evidence
            seen: set[str] = set()
            for cid in case_ids[:5]:
                msgs = get_case_evidence(db, cid)
                for msg in msgs:
                    for p in msg.image_paths:
                        if not p or p in seen:
                            continue
                        seen.add(p)
                        ext = p.rsplit('.', 1)[-1].lower() if '.' in p else ''
                        if f'.{ext}' not in IMAGE_EXTS:
                            files.append(p)
        except Exception:
            log.exception("get_evidence_files failed")
        return files
