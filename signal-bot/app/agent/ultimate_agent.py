import re
import sys
import time
from dataclasses import dataclass, field

from .case_search_agent import CaseSearchAgent
from app.config import load_settings
from app.rag.chroma import create_chroma
from app.llm.client import LLMClient

sys.stdout.reconfigure(encoding='utf-8')

_ATTACH_PATTERN = re.compile(r'\[\[ATTACH:(.*?)\]\]')


def detect_lang(text: str) -> str:
    """Return 'uk' if Cyrillic characters are present, else 'en'."""
    if re.search(r'[а-яіїєґА-ЯІЇЄҐ]', text):
        return "uk"
    return "en"


@dataclass
class AgentResponse:
    text: str
    attachment_urls: list[str] = field(default_factory=list)


class UltimateAgent:
    def __init__(self):
        print("Initializing Ultimate Agent...")
        self.settings = load_settings()
        self.public_url = self.settings.public_url.rstrip('/')
        self.rag = create_chroma(self.settings)
        self.llm = LLMClient(self.settings)
        self.case_agent = CaseSearchAgent(rag=self.rag, llm=self.llm, public_url=self.public_url)
        self.last_load_time = time.time()
        print("Agents loaded.", flush=True)

    def load_agents(self):
        """Reload agents (called on refresh interval).

        Only the Chroma client is refreshed — it needs a new connection so it
        picks up any cases added since startup.  The LLMClient is intentionally
        kept alive: it holds a warm httpx connection pool to the Gemini API and
        re-creating it every 10 minutes causes a cold-start TCP connection that
        is disproportionately vulnerable to transient API hangs.
        """
        self.rag = create_chroma(self.settings)
        self.case_agent = CaseSearchAgent(rag=self.rag, llm=self.llm, public_url=self.public_url)
        self.last_load_time = time.time()

    def answer(self, question, group_id=None, db=None, lang="uk", context: str = "") -> AgentResponse:
        # Refresh agents every 10 minutes
        if time.time() - self.last_load_time > 600:
            print("Refreshing agents...", flush=True)
            try:
                self.load_agents()
            except Exception as e:
                print(f"Error refreshing agents: {e}", flush=True)

        lang = detect_lang(question)
        lang_instruction = "Ukrainian (українська)" if lang == "uk" else "English"

        print(f"\n--- Ultimate: '{question}' (group={group_id}, lang={lang}) ---", flush=True)

        case_ans = "No relevant cases found."
        try:
            case_ans = self.case_agent.answer(question, group_id=group_id, db=db)
        except Exception as e:
            print(f"Case agent error: {e}", flush=True)

        # No matching cases at all → escalate to admin
        if "No relevant cases" in case_ans:
            return AgentResponse(text="[[TAG_ADMIN]]")

        context_block = f"\nRecent chat context (for reference):\n{context}\n" if context.strip() else ""

        # Only open (B1) cases found → escalate but mention the case is being tracked
        if case_ans.startswith("B1_ONLY:"):
            b1_context = case_ans[len("B1_ONLY:"):]
            prompt = f"""You are a concise support bot. A user asked a question. There is a tracked open case for this issue but no solution yet.
{context_block}
Question: "{question}"

Tracked open cases:
{b1_context}

RULES:
1. NO greeting, NO "Вітаю", NO "Based on...".
2. One sentence: state the issue is tracked + include the case link from above.
3. Tag admin at the end with [[TAG_ADMIN]].
4. Respond in {lang_instruction}.
5. Maximum 1 sentence + link + tag. Nothing else.

GOOD: "Ця проблема вже відстежується: https://supportbot.info/case/xxx [[TAG_ADMIN]]"
BAD: "Вітаю! Ми знаємо про цю проблему і вже працюємо над вирішенням."

Answer:"""
            try:
                text = self.llm.chat(prompt=prompt)
                if "[[TAG_ADMIN]]" not in text:
                    text = text + " [[TAG_ADMIN]]"
                return AgentResponse(text=text)
            except Exception as e:
                print(f"Synthesizer error (B1 path): {e}", flush=True)
                return AgentResponse(text="[[TAG_ADMIN]]")

        # Solved cases found (SCRAG / B3) → synthesize direct answer
        # Collect evidence attachment URLs from case context for potential file sharing
        evidence_files = self.case_agent.get_evidence_files(case_ans, db=db)

        file_list_block = ""
        if evidence_files:
            file_list_block = "\n\nAvailable evidence files (use [[ATTACH:url]] to share a file with the user):\n"
            for ef in evidence_files:
                file_list_block += f"- {ef}\n"

        prompt = f"""You are a concise support bot. Answer using the retrieved case if it covers the same core issue.
{context_block}
Question: "{question}"

Retrieved cases:
{case_ans}
{file_list_block}
DECISION RULES — apply in order:
1. Check: does the retrieved case cover the same core issue as the question?
   - "Same core issue" = the underlying problem is the same, even if the user's phrasing is shorter/less detailed.
   - The case may contain more context (e.g. additional troubleshooting steps the previous user took) — that detail does NOT make it a different problem.
   - Use the chat context above to resolve any ambiguities in the question (e.g. "this", "that model", "the same issue").
2. If the case covers a COMPLETELY DIFFERENT TOPIC (e.g. user asked about connectivity, case is about a display setting) → output ONLY "[[TAG_ADMIN]]".
3. If the case covers the same core issue:
   a. Self-service fix (replace a part, change a setting, install software): state the solution in 1-2 sentences + case link. No admin tag.
   b. Needs admin action (user must send a log, file, screenshot, or admin must perform the fix): "<instruction> [[TAG_ADMIN]] <case link>".
4. NO greeting, NO "Based on...", NO "According to...", NO bullet points.
5. Respond in {lang_instruction}.
6. NEVER invent information not in the retrieved case.
7. If a relevant evidence file (PDF, zip, config, etc.) would help the user, include [[ATTACH:url]] for that file. Do NOT attach images.

GOOD: user asks about an error on startup, case is about the same startup error with a fix → same issue → give the solution.
GOOD: "Зайдіть у налаштування та увімкніть відповідну опцію. https://supportbot.info/case/xxx"
GOOD: "Ось конфігураційний файл: [[ATTACH:https://r2.example.com/file.apj]] https://supportbot.info/case/xxx"
GOOD: "Надайте лог з пристрою [[TAG_ADMIN]] https://supportbot.info/case/xxx"
BAD: answer about a network issue when user asked about a software crash.
BAD: bare "[[TAG_ADMIN]]" when the retrieved case covers the same core issue.

Answer:"""

        try:
            raw_text = self.llm.chat(prompt=prompt)
            # Extract [[ATTACH:url]] markers from the response
            attachment_urls = _ATTACH_PATTERN.findall(raw_text)
            clean_text = _ATTACH_PATTERN.sub('', raw_text).strip()
            return AgentResponse(text=clean_text, attachment_urls=attachment_urls)
        except Exception as e:
            print(f"Synthesizer error: {e}", flush=True)
            return AgentResponse(text="[[TAG_ADMIN]]")
