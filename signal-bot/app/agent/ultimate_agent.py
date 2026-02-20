import os
import re
import sys
import time
import google.generativeai as genai

from .case_search_agent import CaseSearchAgent
from app.config import load_settings
from app.rag.chroma import create_chroma
from app.llm.client import LLMClient

sys.stdout.reconfigure(encoding='utf-8')

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)

MODEL_NAME = "gemini-2.0-flash"


def detect_lang(text: str) -> str:
    """Detect language from message text.
    Returns 'uk' if Cyrillic characters present, else 'en'.
    Cyrillic = Ukrainian/Russian; this bot serves Ukrainian users so we default to 'uk'.
    """
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
        self.synthesizer = genai.GenerativeModel(MODEL_NAME)
        self.last_load_time = time.time()
        print("Agents loaded.", flush=True)

    def load_agents(self):
        """Reload agents (called on refresh interval)."""
        self.rag = create_chroma(self.settings)
        self.llm = LLMClient(self.settings)
        self.case_agent = CaseSearchAgent(rag=self.rag, llm=self.llm, public_url=self.public_url)
        self.synthesizer = genai.GenerativeModel(MODEL_NAME)
        self.last_load_time = time.time()

    def answer(self, question, group_id=None, db=None, lang="uk"):
        # Refresh agents every 10 minutes
        if time.time() - self.last_load_time > 600:
            print("Refreshing agents...", flush=True)
            try:
                self.load_agents()
            except Exception as e:
                print(f"Error refreshing agents: {e}", flush=True)

        # Always detect language from the actual message, not admin session
        lang = detect_lang(question)
        lang_instruction = "Ukrainian (українська)" if lang == "uk" else "English"

        print(f"\n--- Ultimate: '{question}' (group={group_id}, lang={lang}) ---", flush=True)

        # Search cases for this group (SCRAG + B3 + B1)
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
                response = self.synthesizer.generate_content(prompt)
                text = response.text.strip()
                # Ensure admin is tagged even if LLM forgot
                if "[[TAG_ADMIN]]" not in text:
                    text = text + " [[TAG_ADMIN]]"
                return text
            except Exception as e:
                print(f"Synthesizer error (B1 path): {e}", flush=True)
                return "[[TAG_ADMIN]]"

        # Solved cases found (SCRAG / B3) → synthesize direct answer
        prompt = f"""You are a concise support bot. Answer directly.

Question: "{question}"

Relevant solved cases:
{case_ans}

RULES:
1. NO greeting, NO "Based on...", NO "According to...".
2. State the ACTUAL solution from the relevant case in 1-2 sentences. Then add the case link.
3. Respond in {lang_instruction}.
4. If the retrieved cases don't actually address the question → output ONLY "[[TAG_ADMIN]]".
5. NO bullet points, NO lists, NO multiple links. Solution + one link only.
6. Do NOT copy example text — write the solution from the case context above.
7. CRITICAL: If the solution requires the user to provide something (a log, a file, a screenshot,
   send something to someone) — that means admin follow-up is needed. In that case add [[TAG_ADMIN]]
   at the end so the admin is notified. Format: "<instruction to user> [[TAG_ADMIN]] <case link>"

GOOD (self-service fix): "Перейдіть у Налаштування → Оновлення та натисніть 'Перевстановити'. https://supportbot.info/case/xxx"
GOOD (needs admin): "Надайте лог з папки /var/log/app/ [[TAG_ADMIN]] https://supportbot.info/case/xxx"
BAD: "Надайте лог" without [[TAG_ADMIN]] — user has nobody to send it to.
BAD: "Вітаю! На основі знайдених кейсів, ось що ми знайшли..."

Answer:"""

        try:
            response = self.synthesizer.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            print(f"Synthesizer error: {e}", flush=True)
            return "[[TAG_ADMIN]]"
