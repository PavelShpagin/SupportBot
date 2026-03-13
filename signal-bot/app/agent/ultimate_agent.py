"""UltimateAgent — parallel CaseSearch + Docs + Keyword agents with synthesizer.

Pipeline:
1. CaseSearchAgent, DocsAgent, and KeywordAgent run in parallel via ThreadPoolExecutor.
2. A synthesizer LLM call (with Google Search grounding) receives all outputs and decides:
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
from .keyword_agent import KeywordAgent
from app.config import load_settings
from app.llm.client import LLMClient, SUBAGENT_CASCADE
from app.rag.chroma import create_chroma

sys.stdout.reconfigure(encoding="utf-8")

log = logging.getLogger(__name__)

_ATTACH_PATTERN = re.compile(r"\[\[ATTACH:(.*?)\]\]")
_CITE_PATTERN = re.compile(r"\[cite:\s*([a-f0-9]{32})\]")


def detect_lang(text: str) -> str:
    if re.search(r"[а-яіїєґА-ЯІЇЄҐ]", text):
        return "uk"
    return "en"


@dataclass
class AgentResponse:
    text: str
    attachment_urls: list[str] = field(default_factory=list)
    sub_agent_results: dict = field(default_factory=dict)


class UltimateAgent:
    def __init__(self):
        log.info("Initializing Ultimate Agent...")
        self.settings = load_settings()
        self.public_url = self.settings.public_url.rstrip("/")
        self.rag = create_chroma(self.settings)
        self.llm = LLMClient(self.settings)
        self.case_agent = CaseSearchAgent(rag=self.rag, llm=self.llm, public_url=self.public_url)
        self.docs_agent = DocsAgent(llm=self.llm)
        self.keyword_agent = KeywordAgent(llm=self.llm, public_url=self.public_url)
        self.last_load_time = time.time()
        log.info("Agents loaded.")

    def load_agents(self):
        """Reload agents on refresh interval.

        Only Chroma is refreshed; LLMClient keeps its warm connection pool.
        """
        self.rag = create_chroma(self.settings)
        self.case_agent = CaseSearchAgent(rag=self.rag, llm=self.llm, public_url=self.public_url)
        self.last_load_time = time.time()

    def answer(self, question, group_id=None, db=None, lang="uk", context: str = "", images: list[tuple[bytes, str]] | None = None, gate_tag: str = "") -> AgentResponse:
        if time.time() - self.last_load_time > 600:
            try:
                self.load_agents()
            except Exception as exc:
                log.warning("Error refreshing agents: %s", exc)

        lang = detect_lang(question)
        lang_instruction = "Ukrainian (українська)" if lang == "uk" else "English"

        log.info("UltimateAgent: '%s' (group=%s, lang=%s)", question[:80], group_id, lang)

        # Run all three agents in parallel
        case_ans = "No relevant cases found."
        docs_ans = "NO_DOCS"
        keyword_ans = "No keyword matches."

        pool = ThreadPoolExecutor(max_workers=3)
        try:
            case_future = pool.submit(
                self.case_agent.answer, question, group_id=group_id, db=db
            )
            docs_future = pool.submit(
                self.docs_agent.answer, question, group_id=group_id, db=db, context=context,
                images=images,
            )
            keyword_future = pool.submit(
                self.keyword_agent.answer, question, group_id=group_id, db=db,
                context=context, images=images,
            )

            futures = [case_future, docs_future, keyword_future]
            try:
                for future in as_completed(futures, timeout=120):
                    try:
                        if future is case_future:
                            case_ans = future.result()
                        elif future is docs_future:
                            docs_ans = future.result()
                        else:
                            keyword_ans = future.result()
                    except Exception as exc:
                        if future is case_future:
                            log.warning("CaseSearchAgent failed: %s", exc)
                        elif future is docs_future:
                            log.warning("DocsAgent failed: %s", exc)
                        else:
                            log.warning("KeywordAgent failed: %s", exc)
            except TimeoutError:
                log.error("Agent futures timed out after 120s; proceeding with partial results")
                for f in futures:
                    if f.done():
                        try:
                            if f is case_future:
                                case_ans = f.result(timeout=0)
                            elif f is docs_future:
                                docs_ans = f.result(timeout=0)
                            else:
                                keyword_ans = f.result(timeout=0)
                        except Exception:
                            pass
                    else:
                        f.cancel()
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        log.info(
            "Agent results: case=%s docs=%s keyword=%s",
            case_ans[:80] if case_ans else "empty",
            docs_ans[:80] if docs_ans else "empty",
            keyword_ans[:80] if keyword_ans else "empty",
        )

        resp = self._synthesize(
            question, case_ans, docs_ans, lang_instruction, context, db, images,
            gate_tag=gate_tag, keyword_ans=keyword_ans,
        )

        # Attach sub-agent results for potential re-synthesis with updated context
        resp.sub_agent_results = {
            "case_ans": case_ans,
            "docs_ans": docs_ans,
            "keyword_ans": keyword_ans,
            "lang_instruction": lang_instruction,
            "gate_tag": gate_tag,
        }

        return resp

    def re_synthesize(self, question: str, new_context: str, prev_response: AgentResponse,
                      db=None, images: list[tuple[bytes, str]] | None = None) -> AgentResponse:
        """Re-run only the synthesizer with updated context but same sub-agent results.

        Used when new messages arrived during synthesis — avoids re-running
        the expensive sub-agent searches.
        """
        sa = prev_response.sub_agent_results
        if not sa:
            log.warning("re_synthesize: no sub_agent_results, returning original response")
            return prev_response

        resp = self._synthesize(
            question, sa["case_ans"], sa["docs_ans"], sa["lang_instruction"],
            new_context, db, images,
            gate_tag=sa.get("gate_tag", ""), keyword_ans=sa.get("keyword_ans", ""),
        )
        resp.sub_agent_results = sa
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
        gate_tag: str = "",
        keyword_ans: str = "",
    ) -> AgentResponse:
        """Synthesize a final answer from all agents' outputs."""
        case_has_results = (
            case_ans
            and "No relevant cases" not in case_ans
        )
        docs_has_results = (
            docs_ans
            and docs_ans not in ("NO_DOCS", "SKIP", "INSUFFICIENT_INFO")
            and not docs_ans.startswith("INSUFFICIENT_INFO")
        )
        keyword_has_results = (
            keyword_ans
            and keyword_ans != "No keyword matches."
        )

        # No agent has useful output
        if not case_has_results and not docs_has_results and not keyword_has_results:
            if gate_tag in ("ongoing_discussion", "statement"):
                log.info("No results for ongoing_discussion/statement — skipping (no admin escalation)")
                return AgentResponse(text="SKIP")
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

        keyword_block = ""
        if keyword_has_results:
            keyword_block = f"\nKEYWORD AGENT (cases found by keyword search in message history):\n{keyword_ans}"

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
{keyword_block}
{docs_block}
{file_list_block}
RULES:
1. RELEVANCE FILTER: You will receive many cases from sub-agents. Most are noise. Use ONLY cases that DIRECTLY answer the user's specific question. If a case is about a tangentially related topic but does NOT address the user's actual problem — SKIP it entirely. Quality over quantity: 1 perfect case > 5 vaguely related ones. Do NOT cobble together a vague answer from tangentially related cases.
2. MULTIPLE QUESTIONS: address EACH sub-question. For parts you cannot answer → add [[TAG_ADMIN]].
3. MULTIPLE SOURCES: freely combine cases, keyword search results, AND docs when it gives a better answer. Cite each source used.
4. CONTEXT AWARENESS: use chat context to resolve "this", "that model", etc. Understand what the user ACTUALLY needs — not just keyword overlap.
6. CITATIONS: list each URL ONCE at the END of your answer. No inline citations, no duplicate URLs. Only cite sources that actually contributed. Format doc citations as: URL (Секція: Y). NEVER use [cite: ...] or [ref: ...] — ALWAYS use full https:// URLs as provided by the agents.
7. BREVITY: answer in 2-5 sentences. No fluff, no filler, no verbose step-by-step tutorials. Give the direct answer, then cite. Users are technical — they don't need hand-holding.
8. NO markdown formatting (no **bold**, no *italic*, no #headers, no `code`). Plain text only. Signal does not render markdown.
9. NO greeting, NO "Вітаю", NO "Based on...", NO "According to...", NO preamble.
10. Respond in {lang_instruction}.
11. NEVER invent information from your own knowledge. You have TWO info sources: (a) sub-agent cases/docs and (b) Google Search. USE BOTH. Google Search is your fact-checking layer — ALWAYS search to verify and enrich your answer. Search for: product specs, model compatibility, error diagnostics, parameter names, wiring details, failure symptoms, firmware info. Even when case data looks sufficient, a quick search often adds critical context. This reduces hallucination significantly.
12. LINK POLICY: use the knowledge you find via Google Search to make your answer more accurate, but do NOT put external URLs in your response. Only include these link types: supportbot.info/case/, docs.google.com, and local device URLs (e.g. pizero2.local). Summarize web-found info in your own words without citing the source URL. Only output [[TAG_ADMIN]] if BOTH cases/docs AND web search yield nothing useful.
13. If evidence files are available, share them with the user via [[ATTACH:url]]. Do NOT attach images.
14. IMAGES: if the user attached an image with visible text (model numbers, labels, error messages, screenshots), treat OCR-extracted text as HARD FACT. Identify the product/component/error confidently.
15. NO REPETITION: if YOUR previous response appears in the LAST ~10 messages of chat context and contains the same case links, do NOT repeat them. Instead, reference your earlier answer or provide only NEW information. If you have nothing new to add, output "SKIP".
16. NEGATIVE EVIDENCE: if KEYWORD AGENT notes that a specific product/model has ZERO mentions in community history, explicitly state this fact. Do NOT extrapolate from general category matches.

Answer:"""

        try:
            raw_text = self.llm.chat_grounded(prompt=prompt, cascade=SUBAGENT_CASCADE, timeout=45.0, images=images)
            attachment_urls = _ATTACH_PATTERN.findall(raw_text)
            clean_text = _ATTACH_PATTERN.sub("", raw_text).strip()
            # Fix [cite: case_id] → proper URL (LLM sometimes hallucinates academic citations)
            clean_text = _CITE_PATTERN.sub(
                lambda m: f"{self.public_url}/case/{m.group(1)}", clean_text
            )
            # Strip markdown formatting (Signal renders it as raw characters)
            clean_text = re.sub(r'\*\*(.+?)\*\*', r'\1', clean_text)  # **bold**
            clean_text = re.sub(r'\*(.+?)\*', r'\1', clean_text)      # *italic*
            clean_text = re.sub(r'`(.+?)`', r'\1', clean_text)        # `code`
            clean_text = re.sub(r'^#{1,6}\s+', '', clean_text, flags=re.MULTILINE)  # # headers
            return AgentResponse(text=clean_text, attachment_urls=attachment_urls)
        except Exception as exc:
            log.exception("Synthesizer LLM call failed")
            return AgentResponse(text="[[TAG_ADMIN]]")
