"""DocsAgent — answers questions from Google Docs loaded in-context.

Uses fetch_doc_recursive() for multimodal (text+images) doc fetching,
and structures prompts for Gemini implicit caching (static docs prefix,
variable query at the end).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.agent.gemini_agent import fetch_doc_recursive
from app.db.queries_mysql import get_group_docs
from app.llm.client import LLMClient, SUBAGENT_CASCADE

log = logging.getLogger(__name__)

_DOC_REFRESH_INTERVAL = 600  # re-fetch docs if older than 10 min


@dataclass
class _DocCacheEntry:
    urls_hash: str
    content_parts: list[Any]
    fetched_at: float


class DocsAgent:
    """Multimodal docs Q&A agent with in-memory doc caching."""

    SYSTEM_PROMPT = (
        "You are a technical support documentation agent. "
        "Answer questions strictly using the provided documentation.\n\n"
        "RULES:\n"
        "1. If the user input is NOT a question or request for help "
        "(greetings, thanks, statements) -> output exactly: SKIP\n"
        "2. If the documentation does NOT cover the topic -> output exactly: INSUFFICIENT_INFO\n"
        "3. If you CAN answer from the docs:\n"
        "   - Provide a clear, concise technical answer.\n"
        "   - MUST cite: [Source: <doc URL>, Section: <heading or description>]\n"
        "   - Quote the key relevant text from the doc.\n"
        "4. Never invent information not present in the documentation.\n"
        "5. Respond in the same language as the question.\n\n"
        "DOCUMENTATION:\n"
    )

    def __init__(self, llm: LLMClient):
        self.llm = llm
        self._cache: dict[str, _DocCacheEntry] = {}

    def _urls_hash(self, urls: list[str]) -> str:
        return hashlib.md5(json.dumps(sorted(urls)).encode()).hexdigest()

    def _get_or_refresh_docs(self, group_id: str, urls: list[str]) -> list[Any]:
        h = self._urls_hash(urls)
        entry = self._cache.get(group_id)
        now = time.time()

        if entry and entry.urls_hash == h and (now - entry.fetched_at) < _DOC_REFRESH_INTERVAL:
            return entry.content_parts

        log.info("DocsAgent: fetching docs for group %s (%d URLs)", group_id[:20], len(urls))
        parts = fetch_doc_recursive(urls, max_depth=1, max_docs=20)
        self._cache[group_id] = _DocCacheEntry(
            urls_hash=h, content_parts=parts, fetched_at=now,
        )
        return parts

    def answer(self, question: str, group_id: str, db, context: str = "") -> str:
        """Answer a question using the group's documentation.

        Returns the agent's raw text: an answer with citations,
        "INSUFFICIENT_INFO", "SKIP", or "NO_DOCS" if no docs configured.
        """
        urls = get_group_docs(db, group_id)
        if not urls:
            return "NO_DOCS"

        doc_parts = self._get_or_refresh_docs(group_id, urls)
        if not doc_parts:
            return "NO_DOCS"

        doc_text_parts = [p for p in doc_parts if isinstance(p, str)]
        doc_text = "\n".join(doc_text_parts)

        context_block = f"\nRecent chat context:\n{context}\n" if context.strip() else ""

        prompt = (
            f"{self.SYSTEM_PROMPT}"
            f"{doc_text}\n\n"
            f"{context_block}"
            f"QUESTION:\n{question}"
        )

        try:
            return self.llm.chat(prompt=prompt, cascade=SUBAGENT_CASCADE, timeout=45.0)
        except Exception as e:
            log.error("DocsAgent error: %s", e)
            return f"INSUFFICIENT_INFO (Error: {e})"
