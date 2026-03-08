"""UltimateAgent — parallel CaseSearch + Docs agents with synthesizer.

Pipeline:
1. CaseSearchAgent and DocsAgent run in parallel via ThreadPoolExecutor.
2. A synthesizer LLM call receives both outputs and decides:
   - Respond with a combined answer (citing sources)
   - Escalate to admin via [[TAG_ADMIN]]
"""
from __future__ import annotations

import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .case_search_agent import CaseSearchAgent
from .docs_agent import DocsAgent
from app.config import load_settings
from app.llm.client import LLMClient, SUBAGENT_CASCADE
from app.rag.chroma import create_chroma

sys.stdout.reconfigure(encoding="utf-8")

log = logging.getLogger(__name__)

_ATTACH_PATTERN = re.compile(r"\[\[ATTACH:(.*?)\]\]")


def detect_lang(text: str) -> str:
    if re.search(r"[а-яіїєґА-ЯІЇЄҐ]", text):
        return "uk"
    return "en"


@dataclass
class AgentResponse:
    text: str
    attachment_urls: list[str] = field(default_factory=list)


class UltimateAgent:
    def __init__(self):
        log.info("Initializing Ultimate Agent...")
        self.settings = load_settings()
        self.public_url = self.settings.public_url.rstrip("/")
        self.rag = create_chroma(self.settings)
        self.llm = LLMClient(self.settings)
        self.case_agent = CaseSearchAgent(rag=self.rag, llm=self.llm, public_url=self.public_url)
        self.docs_agent = DocsAgent(llm=self.llm)
        self.last_load_time = time.time()
        log.info("Agents loaded.")

    def load_agents(self):
        """Reload agents on refresh interval.

        Only Chroma is refreshed; LLMClient keeps its warm connection pool.
        """
        self.rag = create_chroma(self.settings)
        self.case_agent = CaseSearchAgent(rag=self.rag, llm=self.llm, public_url=self.public_url)
        self.last_load_time = time.time()

    def answer(self, question, group_id=None, db=None, lang="uk", context: str = "", images: list[tuple[bytes, str]] | None = None) -> AgentResponse:
        if time.time() - self.last_load_time > 600:
            try:
                self.load_agents()
            except Exception as exc:
                log.warning("Error refreshing agents: %s", exc)

        lang = detect_lang(question)
        lang_instruction = "Ukrainian (українська)" if lang == "uk" else "English"

        log.info("UltimateAgent: '%s' (group=%s, lang=%s)", question[:80], group_id, lang)

        # Run both agents in parallel
        case_ans = "No relevant cases found."
        docs_ans = "NO_DOCS"

        pool = ThreadPoolExecutor(max_workers=2)
        try:
            case_future = pool.submit(
                self.case_agent.answer, question, group_id=group_id, db=db
            )
            docs_future = pool.submit(
                self.docs_agent.answer, question, group_id=group_id, db=db, context=context
            )

            try:
                for future in as_completed([case_future, docs_future], timeout=120):
                    try:
                        if future is case_future:
                            case_ans = future.result()
                        else:
                            docs_ans = future.result()
                    except Exception as exc:
                        if future is case_future:
                            log.warning("CaseSearchAgent failed: %s", exc)
                        else:
                            log.warning("DocsAgent failed: %s", exc)
            except TimeoutError:
                log.error("Agent futures timed out after 120s; proceeding with partial results")
                for f in [case_future, docs_future]:
                    if f.done():
                        try:
                            if f is case_future:
                                case_ans = f.result(timeout=0)
                            else:
                                docs_ans = f.result(timeout=0)
                        except Exception:
                            pass
                    else:
                        f.cancel()
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        log.info(
            "Agent results: case=%s docs=%s",
            case_ans[:80] if case_ans else "empty",
            docs_ans[:80] if docs_ans else "empty",
        )

        resp = self._synthesize(
            question, case_ans, docs_ans, lang_instruction, context, db, images
        )

        return resp

    def _synthesize(
        self,
        question: str,
        case_ans: str,
        docs_ans: str,
        lang_instruction: str,
        context: str,
        db,
        images: list[tuple[bytes, str]] | None = None,
    ) -> AgentResponse:
        """Synthesize a final answer from both agents' outputs."""
        case_has_results = (
            case_ans
            and "No relevant cases" not in case_ans
        )
        docs_has_results = (
            docs_ans
            and docs_ans not in ("NO_DOCS", "SKIP", "INSUFFICIENT_INFO")
            and not docs_ans.startswith("INSUFFICIENT_INFO")
        )

        # Neither agent has useful output → escalate
        if not case_has_results and not docs_has_results:
            return AgentResponse(text="[[TAG_ADMIN]]")

        context_block = f"\nRecent chat context (for reference):\n{context}\n" if context.strip() else ""

        # Collect evidence files for potential attachment sharing
        evidence_files = self.case_agent.get_evidence_files(case_ans, db=db) if case_has_results else []
        file_list_block = ""
        if evidence_files:
            file_list_block = "\n\nAvailable evidence files (use [[ATTACH:url]] to share a file with the user):\n"
            for ef in evidence_files:
                file_list_block += f"- {ef}\n"

        # Build source blocks
        case_block = ""
        if case_has_results:
            if case_ans.startswith("B1_ONLY:"):
                case_block = f"\nCASE AGENT (recommendation cases — unconfirmed):\n{case_ans[len('B1_ONLY:'):]}"
            else:
                case_block = f"\nCASE AGENT (solved cases):\n{case_ans}"

        docs_block = ""
        if docs_has_results:
            docs_block = f"\nDOCS AGENT (from documentation):\n{docs_ans}"

        # Embed image markers in the question text if images are present
        question_with_images = question
        if images:
            markers = " ".join(f"[[IMG:{j}]]" for j in range(len(images)))
            question_with_images = f"{question}\n{markers}"

        prompt = f"""You are a concise support bot. Synthesize a final answer using the information from the sub-agents below.
{context_block}
Question: "{question_with_images}"
{case_block}
{docs_block}
{file_list_block}
DECISION RULES — apply in order:
1. If the question contains MULTIPLE sub-questions, address EACH one separately. For any sub-question you cannot answer from the provided sources, add [[TAG_ADMIN]] so an expert can help with the remaining parts.
2. If a sub-agent found a SOLVED case or documentation that covers the same core issue as the question:
   - "Same core issue" = the underlying problem is the same, even if phrased differently.
   - Use chat context to resolve ambiguities (e.g. "this", "that model", "the same issue").
3. If the information covers a COMPLETELY DIFFERENT TOPIC from ALL questions → output ONLY "[[TAG_ADMIN]]".
4. If the information covers the same core issue:
   a. Self-service fix: state the solution concisely + case/doc link. No admin tag.
   b. Needs admin action: "<instruction> [[TAG_ADMIN]] <link>".
5. If only OPEN/tracked cases exist (no solution yet): one sentence stating the issue is tracked + case link + [[TAG_ADMIN]].
6. If BOTH agents returned useful info, combine the best answer. Prefer documentation for how-to, prefer cases for known bugs/fixes.
7. Include source citations (case links and/or doc URLs) ONLY when they are relevant and actually helped answer a question. Do NOT include citations that are unrelated to the question. If citing a specific section: URL (Секція: Y).
8. NO greeting, NO "Вітаю", NO "Based on...", NO "According to...", NO bullet points.
9. Respond in {lang_instruction}.
10. NEVER invent information not provided by the agents.
11. If a relevant evidence file (PDF, zip, config) would help, include [[ATTACH:url]]. Do NOT attach images.

Answer:"""

        try:
            raw_text = self.llm.chat(prompt=prompt, cascade=SUBAGENT_CASCADE, timeout=45.0, images=images)
            attachment_urls = _ATTACH_PATTERN.findall(raw_text)
            clean_text = _ATTACH_PATTERN.sub("", raw_text).strip()
            return AgentResponse(text=clean_text, attachment_urls=attachment_urls)
        except Exception as exc:
            log.exception("Synthesizer LLM call failed")
            return AgentResponse(text="[[TAG_ADMIN]]")
