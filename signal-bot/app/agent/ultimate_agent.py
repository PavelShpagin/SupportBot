import re
import sys
import time

from .case_search_agent import CaseSearchAgent
from app.config import load_settings
from app.rag.chroma import create_chroma
from app.llm.client import LLMClient

sys.stdout.reconfigure(encoding='utf-8')


def detect_lang(text: str) -> str:
    """Return 'uk' if Cyrillic characters are present, else 'en'."""
    if re.search(r'[а-яіїєґА-ЯІЇЄҐ]', text):
        return "uk"
    return "en"


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

    def answer(self, question, group_id=None, db=None, lang="uk"):
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
            return "[[TAG_ADMIN]]"

        # Only open (B1) cases found → escalate but mention the case is being tracked
        if case_ans.startswith("B1_ONLY:"):
            b1_context = case_ans[len("B1_ONLY:"):]
            prompt = f"""You are a concise support bot. A user asked a question. There is a tracked open case for this issue but no solution yet.

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
                return text
            except Exception as e:
                print(f"Synthesizer error (B1 path): {e}", flush=True)
                return "[[TAG_ADMIN]]"

        # Solved cases found (SCRAG / B3) → synthesize direct answer
        prompt = f"""You are a concise support bot. Answer using the retrieved case if it covers the same core issue.

Question: "{question}"

Retrieved cases:
{case_ans}

DECISION RULES — apply in order:
1. Check: does the retrieved case cover the same core issue as the question?
   - "Same core issue" = the underlying problem is the same, even if the user's phrasing is shorter/less detailed.
   - The case may contain more context (e.g. additional troubleshooting steps the previous user took) — that detail does NOT make it a different problem.
2. If the case covers a COMPLETELY DIFFERENT TOPIC (e.g. user asked about battery, case is about GPS settings) → output ONLY "[[TAG_ADMIN]]".
3. If the case covers the same core issue:
   a. Self-service fix (replace a part, change a setting, install software): state the solution in 1-2 sentences + case link. No admin tag.
   b. Needs admin action (user must send a log, file, screenshot, or admin must perform the fix): "<instruction> [[TAG_ADMIN]] <case link>".
4. NO greeting, NO "Based on...", NO "According to...", NO bullet points.
5. Respond in {lang_instruction}.
6. NEVER invent information not in the retrieved case.

GOOD: user asks "burned battery", case is about "battery burned, RadioMaster 5000mah works" → same issue → give the solution.
GOOD: "Зайдіть у «налаштування» → «tracking» → «on». https://supportbot.info/case/xxx"
GOOD: "Надайте лог з /var/log/app/ [[TAG_ADMIN]] https://supportbot.info/case/xxx"
BAD: answer about GPS when user asked about battery.
BAD: bare "[[TAG_ADMIN]]" when the retrieved case covers the same core issue.

Answer:"""

        try:
            return self.llm.chat(prompt=prompt)
        except Exception as e:
            print(f"Synthesizer error: {e}", flush=True)
            return "[[TAG_ADMIN]]"
