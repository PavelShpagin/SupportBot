"""KeywordAgent — keyword-based case retrieval.

Pipeline:
1. LLM #1 (fast) extracts search keywords from the user's question
2. LIKE search on raw_messages → message_ids
3. JOIN case_evidence → find cases containing those messages
4. LLM #2 (standard cascade) synthesizes a sub-answer from matched cases
5. Negative evidence appended for keywords with 0 mentions
"""
from __future__ import annotations

import logging
import sys
from typing import Any, Optional

sys.stdout.reconfigure(encoding="utf-8")

log = logging.getLogger(__name__)


class KeywordAgent:
    def __init__(self, llm, public_url: str = "https://supportbot.info"):
        self.llm = llm
        self.public_url = public_url

    def answer(
        self,
        question: str,
        group_id: Optional[str] = None,
        db=None,
        context: str = "",
        images: list[tuple[bytes, str]] | None = None,
    ) -> str:
        if not group_id or db is None:
            return "No keyword matches."

        # Step 1: LLM extracts keywords
        try:
            kw = self.llm.extract_keywords(message=question)
        except Exception:
            log.exception("KeywordAgent: keyword extraction failed")
            return "No keyword matches."

        all_terms = list(dict.fromkeys(kw.keywords))  # dedupe, preserve order
        if not all_terms:
            log.info("KeywordAgent: no keywords extracted")
            return "No keyword matches."

        log.info("KeywordAgent: keywords=%s", all_terms[:5])

        # Resolve union group_ids
        try:
            from app.db import get_union_group_ids
            union_gids = get_union_group_ids(db, group_id)
        except Exception:
            union_gids = [group_id]

        # Step 2: LIKE search on raw_messages
        from app.db.queries_mysql import (
            search_messages_by_terms,
            find_cases_by_message_ids,
            count_term_in_messages,
        )

        try:
            matched_msg_ids = search_messages_by_terms(db, all_terms, union_gids, limit=50)
        except Exception:
            log.exception("KeywordAgent: message search failed")
            matched_msg_ids = []

        log.info("KeywordAgent: %d message hits for %d terms", len(matched_msg_ids), len(all_terms))

        # Step 3: Find cases via case_evidence
        cases: list[dict] = []
        if matched_msg_ids:
            try:
                cases = find_cases_by_message_ids(db, matched_msg_ids)
            except Exception:
                log.exception("KeywordAgent: case lookup failed")

        log.info("KeywordAgent: %d cases found via keyword search", len(cases))

        # Step 5: Negative evidence — check if any keyword has zero mentions
        negative_notes: list[str] = []
        for term in all_terms[:5]:
            try:
                count = count_term_in_messages(db, term, union_gids)
                if count == 0:
                    negative_notes.append(
                        f"NOTE: '{term}' has ZERO mentions across community message history."
                    )
            except Exception:
                pass

        if not cases and not negative_notes:
            return "No keyword matches."

        # Step 4: LLM #2 synthesizes sub-answer from cases + question + context
        if cases:
            from app.llm.client import SUBAGENT_CASCADE
            from app.llm import prompts as P

            cases_text = self._format_cases(cases)
            prompt = (
                f"{P.P_KEYWORD_SYNTH_SYSTEM}\n\n"
                f"Питання користувача: \"{question}\"\n\n"
            )
            if context.strip():
                prompt += f"Контекст чату (останні повідомлення):\n{context}\n\n"
            prompt += f"Знайдені кейси через пошук за ключовими словами:\n{cases_text}\n"

            try:
                sub_answer = self.llm.chat(
                    prompt=prompt,
                    cascade=SUBAGENT_CASCADE,
                    timeout=30.0,
                    images=images,
                )
            except Exception:
                log.exception("KeywordAgent: synthesis LLM failed")
                sub_answer = cases_text  # fallback: raw case list
        else:
            sub_answer = ""

        # Combine sub-answer + negative evidence
        parts = []
        if sub_answer:
            parts.append(sub_answer)
        if negative_notes:
            parts.append("\n".join(negative_notes))

        return "\n\n".join(parts) if parts else "No keyword matches."

    def _format_cases(self, cases: list[dict]) -> str:
        lines: list[str] = []
        for c in cases[:15]:  # cap to avoid huge prompts
            status = c.get("status", "recommendation")
            prefix = "[Solved]" if status == "solved" else "[Recommendation]"
            link = f"{self.public_url}/case/{c['case_id']}"
            lines.append(
                f"- {prefix} {c.get('problem_title', '???')}\n"
                f"  Problem: {c.get('problem_summary', '')[:200]}\n"
                f"  Solution: {c.get('solution_summary', '')[:300]}\n"
                f"  Link: {link}"
            )
        return "\n".join(lines)
