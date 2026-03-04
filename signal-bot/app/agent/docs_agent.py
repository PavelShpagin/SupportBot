"""DocsAgent — Google Docs Q&A with implicit caching.

Fetches group-specific documentation from Google Docs, sends full content
as context to Gemini, and returns answers with source citations.

Prompt is structured for implicit caching: system instruction + docs (static
prefix) followed by chat context + question (variable suffix).
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from app.llm.client import LLMClient, SUBAGENT_CASCADE

log = logging.getLogger(__name__)

_SYSTEM_INSTRUCTION = """You are a technical support automation system. Your goal is to strictly filter and answer questions based on the provided documentation.

INPUT CLASSIFICATION & BEHAVIOR:

1. **ANALYZE**: Is the user input a QUESTION or a REQUEST FOR HELP?
   - **NO** (Greetings like "Hi", Gratitude like "Thanks", Statements like "Here is a log", Random phrases):
     -> Output exactly: "SKIP"
   - **YES**: Proceed to step 2.

2. **EVALUATE**: Do you have the information in the provided CONTEXT to answer it?
   - **NO** (The topic is not covered, or you cannot perform the requested action like analyzing a log file):
     -> Output exactly: "INSUFFICIENT_INFO"
   - **YES**:
     -> Provide a clear, technical answer.
     -> You MUST cite the specific Source URL and section.
     -> Format: "Answer... [Source: URL, Section: ...]"
"""

# How long before re-fetching docs (seconds)
_DOC_CACHE_TTL = 600  # 10 minutes


class DocsAgent:
    """Answers questions using Google Docs content as context.

    Docs are cached in-memory per group with hash-based invalidation.
    Prompt ordering enables Gemini implicit caching (static prefix).
    """

    def __init__(self, llm: LLMClient):
        self.llm = llm
        self._cache: dict[str, _CacheEntry] = {}

    def answer(
        self,
        question: str,
        group_id: str,
        db: Any,
        context: str = "",
    ) -> str:
        """Answer a question using group docs.

        Returns one of:
          - An answer with [Source: ...] citations
          - "SKIP" (not a question)
          - "INSUFFICIENT_INFO" (not in docs)
          - "NO_DOCS" (group has no docs configured)
        """
        from app.db.queries_mysql import get_group_docs

        docs_urls = get_group_docs(db, group_id)
        if not docs_urls:
            return "NO_DOCS"

        content_parts = self._get_or_refresh(group_id, docs_urls)
        if not content_parts:
            return "NO_DOCS"

        prompt, images = self._build_prompt(content_parts, context, question)

        try:
            return self.llm.chat(
                prompt=prompt,
                cascade=SUBAGENT_CASCADE,
                timeout=60.0,
                images=images if images else None,
            )
        except Exception as exc:
            log.exception("DocsAgent LLM call failed for group %s", group_id)
            return f"INSUFFICIENT_INFO (Error: {exc})"

    def _get_or_refresh(
        self, group_id: str, docs_urls: list[str]
    ) -> list[str | dict]:
        """Return cached doc content, re-fetching if URLs changed or TTL expired."""
        urls_hash = hashlib.md5("|".join(sorted(docs_urls)).encode()).hexdigest()
        entry = self._cache.get(group_id)

        if entry and entry.urls_hash == urls_hash and (time.time() - entry.fetched_at) < _DOC_CACHE_TTL:
            return entry.parts

        from app.agent.gemini_agent import fetch_doc_recursive

        log.info("DocsAgent: fetching docs for group %s (%d URLs)", group_id, len(docs_urls))
        try:
            parts = fetch_doc_recursive(docs_urls, max_depth=1, max_docs=10)
        except Exception:
            log.exception("DocsAgent: failed to fetch docs for group %s", group_id)
            if entry:
                return entry.parts
            return []

        self._cache[group_id] = _CacheEntry(
            urls_hash=urls_hash,
            parts=parts,
            fetched_at=time.time(),
        )
        return parts

    @staticmethod
    def _build_prompt(
        content_parts: list[str | dict],
        context: str,
        question: str,
    ) -> tuple[str, list[tuple[bytes, str]]]:
        """Build prompt with interleaved [[IMG:N]] markers for doc images.

        Returns (prompt_text, images_list). Images from Google Docs are placed
        at their natural positions in the document flow using markers.
        """
        doc_text_parts: list[str] = []
        images: list[tuple[bytes, str]] = []
        img_idx = 0

        for part in content_parts:
            if isinstance(part, str):
                doc_text_parts.append(part)
            elif isinstance(part, dict) and "data" in part:
                mime = part.get("mime_type", "image/jpeg")
                images.append((part["data"], mime))
                doc_text_parts.append(f"[[IMG:{img_idx}]]")
                img_idx += 1
            else:
                doc_text_parts.append(str(part))

        docs_block = "\n".join(doc_text_parts)
        context_block = f"\nRecent chat context:\n{context}\n" if context.strip() else ""

        prompt = (
            f"{_SYSTEM_INSTRUCTION}\n\n"
            f"DOCUMENTATION:\n{docs_block}\n\n"
            f"{context_block}"
            f"QUESTION:\n{question}"
        )
        return prompt, images


class _CacheEntry:
    __slots__ = ("urls_hash", "parts", "fetched_at")

    def __init__(self, urls_hash: str, parts: list, fetched_at: float):
        self.urls_hash = urls_hash
        self.parts = parts
        self.fetched_at = fetched_at
