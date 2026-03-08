"""DocsAgent: answers questions using Google Docs loaded all-in-context.

Fetches docs recursively from URLs stored in chat_groups.docs_urls,
passes them as multimodal content (text + images) to Gemini, and
returns an answer with citations or INSUFFICIENT_INFO / SKIP.

Prompt ordering is designed for implicit caching: the static docs
prefix stays the same across requests for the same group, while the
variable query goes last.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from app.agent.gemini_agent import fetch_doc_recursive
from app.db.queries_mysql import get_group_docs
from app.llm.client import LLMClient, SUBAGENT_CASCADE

log = logging.getLogger(__name__)

_DOC_REFRESH_INTERVAL_S = 600  # re-fetch docs every 10 minutes

DOCS_SYSTEM_PROMPT = """You are a technical support automation system. Your goal is to strictly filter and answer questions based ONLY on the provided documentation.

INPUT CLASSIFICATION & BEHAVIOR:

1. **ANALYZE**: Is the user input a QUESTION or a REQUEST FOR HELP?
   - **NO** (Greetings, gratitude, statements, random phrases):
     -> Output exactly: "SKIP"
   - **YES**: Proceed to step 2.

2. **EVALUATE**: Do you have the information in the provided CONTEXT to answer it?
   - **NO** (The topic is not covered, or you cannot perform the requested action):
     -> Output exactly: "INSUFFICIENT_INFO"
   - **YES**:
     -> Provide a clear, technical answer.
     -> You MUST cite the source URL.
     -> If the answer comes from a specific section, use: URL (Section: section name) or URL (Секція: назва секції)
     -> If the answer comes from the document generally (no specific section), use: URL (document title)

3. **LANGUAGE**: Respond in the same language as the question.

NEVER invent information not present in the provided documents.
"""


class _DocsCacheEntry:
    __slots__ = ("urls_hash", "content_parts", "fetched_at")

    def __init__(self, urls_hash: str, content_parts: list[Any], fetched_at: float):
        self.urls_hash = urls_hash
        self.content_parts = content_parts
        self.fetched_at = fetched_at


class DocsAgent:
    """Answers questions using per-group Google Docs loaded fully in context."""

    def __init__(self, llm: LLMClient):
        self.llm = llm
        self._cache: dict[str, _DocsCacheEntry] = {}

    def _urls_hash(self, urls: list[str]) -> str:
        return hashlib.sha256(json.dumps(sorted(urls)).encode()).hexdigest()[:16]

    def invalidate_cache(self, group_id: str) -> None:
        """Force next query for this group to re-fetch docs."""
        self._cache.pop(group_id, None)

    def _get_or_refresh_docs(self, group_id: str, urls: list[str]) -> list[Any]:
        h = self._urls_hash(urls)
        entry = self._cache.get(group_id)
        now = time.time()

        if entry and entry.urls_hash == h and (now - entry.fetched_at) < _DOC_REFRESH_INTERVAL_S:
            return entry.content_parts

        log.info("Fetching docs for group %s (%d URLs)", group_id[:20], len(urls))
        try:
            parts = fetch_doc_recursive(urls, max_docs=50)
        except Exception as exc:
            log.error("Failed to fetch docs for group %s: %s", group_id[:20], exc)
            if entry:
                return entry.content_parts
            return [f"[Error loading documentation: {exc}]"]

        self._cache[group_id] = _DocsCacheEntry(urls_hash=h, content_parts=parts, fetched_at=now)
        log.info("Cached %d doc parts for group %s", len(parts), group_id[:20])
        return parts

    @staticmethod
    def _build_prompt_with_images(
        content_parts: list[Any], context: str, question: str,
    ) -> tuple[str, list[tuple[bytes, str]]]:
        """Build prompt text with [[IMG:N]] markers and a parallel images list.

        Doc images are placed at their natural positions in the document flow.
        """
        doc_text_segments: list[str] = []
        images: list[tuple[bytes, str]] = []
        img_idx = 0

        for part in content_parts:
            if isinstance(part, str):
                doc_text_segments.append(part)
            elif isinstance(part, dict) and "data" in part:
                mime = part.get("mime_type", "image/jpeg")
                images.append((part["data"], mime))
                doc_text_segments.append(f"[[IMG:{img_idx}]]")
                img_idx += 1
            else:
                doc_text_segments.append(str(part))

        docs_block = "\n".join(doc_text_segments)

        variable_section = ""
        if context.strip():
            variable_section += f"\nRecent chat context:\n{context}\n"
        variable_section += f"\nQUESTION:\n{question}"

        prompt = f"{DOCS_SYSTEM_PROMPT}\n\nDOCUMENTATION:\n{docs_block}\n{variable_section}"
        return prompt, images

    def answer(self, question: str, group_id: str, db: Any, context: str = "",
               images: list[tuple[bytes, str]] | None = None) -> str:
        """Answer a question using the group's documentation.

        Returns the answer text, "INSUFFICIENT_INFO", "SKIP", or "NO_DOCS".
        """
        # Gather docs from all groups in the union
        try:
            from app.db import get_union_group_ids
            union_gids = get_union_group_ids(db, group_id)
        except Exception:
            union_gids = [group_id]
        seen: set[str] = set()
        urls: list[str] = []
        for gid in union_gids:
            for u in get_group_docs(db, gid):
                if u not in seen:
                    seen.add(u)
                    urls.append(u)
        if not urls:
            return "NO_DOCS"

        content_parts = self._get_or_refresh_docs(group_id, urls)

        prompt, doc_images = self._build_prompt_with_images(content_parts, context, question)

        # Merge user images (from the question) after doc images
        all_images = list(doc_images) if doc_images else []
        if images:
            offset = len(all_images)
            markers = " ".join(f"[[IMG:{offset + j}]]" for j in range(len(images)))
            prompt = f"{prompt}\n\nUser attached images:\n{markers}"
            all_images.extend(images)

        try:
            return self.llm.chat(
                prompt=prompt,
                cascade=SUBAGENT_CASCADE,
                timeout=60.0,
                images=all_images if all_images else None,
            )
        except Exception as exc:
            log.error("DocsAgent LLM call failed: %s", exc)
            return f"INSUFFICIENT_INFO (Error: {exc})"
