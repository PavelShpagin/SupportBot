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

        # Search cases for this group
        case_ans = "No relevant cases found."
        try:
            case_ans = self.case_agent.answer(question, group_id=group_id, db=db)
        except Exception as e:
            print(f"Case agent error: {e}", flush=True)

        # No matching cases → escalate to admin
        if "No relevant cases" in case_ans:
            return "[[TAG_ADMIN]]"

        # Synthesize a direct answer in the message language
        prompt = f"""You are a support bot. Answer the user's question directly and concisely.

Question: "{question}"

Relevant past cases:
{case_ans}

RULES:
1. Give a DIRECT ANSWER in 1-3 sentences. No introductions, no "Based on..." or "According to...".
2. State the solution directly, then add ONE case link at the end (if available).
3. Respond in {lang_instruction}.
4. If the cases don't actually address the question → output ONLY "[[TAG_ADMIN]]".
5. NO bullet points, NO lists, NO multiple links. Just the answer + one link.

GOOD: "Використовуйте термосумку для захисту дрона від замерзання. https://supportbot.info/case/xxx"
BAD: "Based on the cases found... Here are some links: [link1], [link2]"

Answer:"""

        try:
            response = self.synthesizer.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            print(f"Synthesizer error: {e}", flush=True)
            return "[[TAG_ADMIN]]"
