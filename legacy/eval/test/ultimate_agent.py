"""
SupportBot Ultimate Agent v2

Architecture: Chat-First with Enrichment
    - Chat search is PRIMARY (96.9% accuracy standalone)
    - Cases and Docs are SUPPLEMENTARY (enrich, don't replace)

Key Insight: Chat history contains actual solved problems.
             Docs are generic. Cases are extracted abstractions.
             Prioritize the source that works best.

Output formats:
    - SKIP: Message is noise
    - ANSWER: Response with HTML citations
    - INSUFFICIENT_INFO: Needs human review
"""

import os
import sys
import json
import re
import google.generativeai as genai
from pathlib import Path

# Fix encoding for Windows
sys.stdout.reconfigure(encoding='utf-8')

# Load environment
def _load_env():
    env_path = Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip("'\"")
        os.environ.setdefault(k, v)

_load_env()
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    raise ValueError("GOOGLE_API_KEY not set")
genai.configure(api_key=GOOGLE_API_KEY)

MODEL = "gemini-2.0-flash"

# Import agents
from gemini_agent import GeminiAgent, build_context_from_description
from chat_search_agent import ChatSearchAgent
from case_search_agent import CaseSearchAgent


class UltimateAgentV2:
    """
    Ultimate SupportBot Agent v2
    
    Strategy: Chat-First with Smart Enrichment
    
    The chat search achieves 96.9% accuracy on its own.
    We use cases/docs only when chat has no answer, or to supplement.
    """
    
    def __init__(self, public_url="http://localhost:3000"):
        print("Initializing Ultimate Agent v2...")
        self.public_url = public_url
        
        # Find project root (directory containing 'test' folder)
        script_dir = Path(__file__).parent.resolve()
        if script_dir.name == "test":
            project_root = script_dir.parent
        else:
            project_root = script_dir
        
        # 1. Chat Agent (PRIMARY - 96.9% accuracy)
        chat_index_path = project_root / "test" / "data" / "chat_index.pkl"
        if chat_index_path.exists():
            self.chat_agent = ChatSearchAgent(str(chat_index_path), public_url=public_url)
            print(f"✓ Chat Agent loaded ({len(self.chat_agent.tool.messages)} messages)")
        else:
            self.chat_agent = None
            print(f"✗ Chat Agent not available (no index at {chat_index_path})")
        
        # 2. Case Agent (SECONDARY - structured solved tickets)
        cases_path = project_root / "test" / "data" / "signal_cases_structured.json"
        if cases_path.exists():
            self.case_agent = CaseSearchAgent(str(cases_path), public_url=public_url)
            print(f"✓ Case Agent loaded ({len(self.case_agent.cases)} cases)")
        else:
            self.case_agent = None
            print(f"✗ Case Agent not available (no cases at {cases_path})")
        
        # 3. Docs Agent (TERTIARY - official documentation)
        docs_path = project_root / "paper" / "repo" / "docs.txt"
        if not docs_path.exists():
            docs_path = project_root / "test" / "description.txt"
        if docs_path.exists():
            context = build_context_from_description(str(docs_path))
            self.docs_agent = GeminiAgent(context)
            print("✓ Docs Agent loaded")
        else:
            self.docs_agent = None
            print(f"✗ Docs Agent not available (no docs at {docs_path})")
        
        # 4. Synthesis Model
        self.model = genai.GenerativeModel(MODEL)
        
        print("Ultimate Agent v2 ready.\n")
    
    # =========================================================================
    # NOISE DETECTION
    # =========================================================================
    
    NOISE_PATTERNS = {
        # Exact matches (lowercase)
        "exact": [
            # Greetings
            "привіт", "hi", "hello", "добрий день", "добрий вечір", "доброго дня",
            "вітаю", "здоровенькі", "добрий ранок", "доброго ранку",
            # Thanks
            "дякую", "дякуємо", "спасибі", "спасибо", "thanks", "thx", "thank you",
            "дуже дякую", "щиро дякую", "велике спасибі",
            # Acknowledgments
            "ok", "ок", "добре", "зрозуміло", "понял", "понятно", "поняв", "ясно",
            "зрозумів", "зрозуміла", "буду знати", "врахую", "прийнято",
            # Reactions
            "👍", "👌", "+", "++", "+++", "так", "ні", "yes", "no",
            "круто", "супер", "клас", "класно", "норм", "нормально", "чудово",
            "ага", "угу", "йой", "ого", "вау",
            # Short confirmations that need context
            "спробуємо", "спробую", "буду пробувати", "продивлюсь",
            "буду дивитись", "подивлюсь", "гляну", "перевірю", "перевіримо",
            "в процесі", "в процесі якраз", "працюю", "роблю",
        ],
        # Prefix patterns
        "prefixes": [
            "дякую за", "спасибі за", "thanks for", "thank you for",
            "зрозумів,", "зрозуміло,", "ок,", "добре,", "ясно,",
            "чудова ", "відмінно", "супер,",
        ],
        # Suffix patterns (things that end sentences in gratitude/acknowledgment)
        "suffixes": [
            "дякую!", "дякую", "спасибі!", "дуже дякую!", "щиро дякую!",
            "дякую🤝", "дякую 👍",
        ],
        # Regex patterns for context-dependent messages
        "context_dependent_phrases": [
            "в процесі", "буду", "спробуємо", "подивлюсь", "поняв",
            "зрозумів", "дякую", "спасибі",
        ],
    }
    
    def _is_noise(self, message: str) -> bool:
        """
        Detect if message is pure noise (greeting, thanks, reaction, acknowledgment).
        
        More aggressive detection for context-dependent messages that
        don't contain enough information to answer meaningfully.
        
        Returns True if message should be SKIPPED.
        """
        # Strip and clean message (remove attachments for analysis)
        msg_clean = message.strip()
        # Remove attachment markers for analysis
        import re
        msg_no_attach = re.sub(r'\[ATTACHMENT[^\]]*\]', '', msg_clean).strip()
        msg = msg_no_attach.lower()
        
        # Empty or very short (after removing attachments)
        if len(msg) < 2:
            return True
        
        # Exact matches
        if msg in self.NOISE_PATTERNS["exact"]:
            return True
        
        # Prefix matches
        for prefix in self.NOISE_PATTERNS["prefixes"]:
            if msg.startswith(prefix):
                return True
        
        # Suffix matches
        for suffix in self.NOISE_PATTERNS["suffixes"]:
            if msg.endswith(suffix):
                # Short messages ending with thanks are acknowledgments
                if len(msg) < 50:
                    return True
        
        # Context-dependent short messages
        if len(msg) < 40:
            # Check if it's a context-dependent acknowledgment
            for phrase in self.NOISE_PATTERNS["context_dependent_phrases"]:
                if phrase in msg:
                    # No question mark = likely acknowledgment
                    if "?" not in msg:
                        return True
        
        # Short message without question indicators
        if len(msg) < 25:
            has_question = "?" in msg
            has_url = "http" in msg
            has_numbers = any(c.isdigit() for c in msg)
            has_technical = any(t in msg for t in [
                "error", "помилка", "не працює", "не работает", "проблема",
                "налаштування", "параметр", "прошивка", "firmware"
            ])
            
            if not (has_question or has_url or has_numbers or has_technical):
                return True
        
        # Off-topic political/personal names (common false triggers)
        off_topic = ["порошенко", "зеленський", "байден", "путін", "трамп"]
        if any(ot in msg for ot in off_topic) and len(msg) < 50:
            return True
        
        return False
    
    # =========================================================================
    # MAIN ANSWER METHOD
    # =========================================================================
    
    def answer(self, question: str, group_id: str = None, db=None) -> str:
        """
        Answer a question using chat-first strategy with enrichment.
        
        Strategy:
        1. Check for noise → SKIP
        2. Search chat history (PRIMARY)
        3. Search cases (if chat has no answer)
        4. Search docs (if cases have no answer)
        5. Synthesize with priority: Chat > Cases > Docs
        
        Returns:
            - Answer string with HTML citations
            - "SKIP" for noise
            - "INSUFFICIENT_INFO" when no relevant info found
        """
        # Step 0: Noise filter
        if self._is_noise(question):
            return "SKIP"
        
        # Step 1: Chat search (PRIMARY)
        chat_answer = None
        chat_found = False
        if self.chat_agent:
            try:
                result = self.chat_agent.answer(question, return_details=True, html_citations=False)
                if isinstance(result, dict):
                    chat_answer = result.get("answer", "")
                    chat_found = "No relevant" not in chat_answer and "Error" not in chat_answer
                else:
                    chat_answer = result
                    chat_found = "No relevant" not in chat_answer
            except Exception as e:
                chat_answer = f"Error: {e}"
        
        # Step 2: Case search (SECONDARY)
        case_answer = None
        case_found = False
        if self.case_agent:
            try:
                case_answer = self.case_agent.answer(question, html_citations=False)
                case_found = "No relevant" not in case_answer and "Error" not in case_answer
            except Exception as e:
                case_answer = f"Error: {e}"
        
        # Step 3: Docs search (TERTIARY)
        docs_answer = None
        docs_found = False
        if self.docs_agent:
            try:
                docs_answer = self.docs_agent.answer(question)
                docs_found = "SKIP" not in docs_answer and "INSUFFICIENT" not in docs_answer and "Error" not in docs_answer
            except Exception as e:
                docs_answer = f"Error: {e}"
        
        # Step 4: Decision logic (CHAT-FIRST)
        
        # If chat found answer, use it (possibly enriched)
        if chat_found:
            return self._format_chat_first_answer(question, chat_answer, case_answer if case_found else None, docs_answer if docs_found else None)
        
        # If only cases found
        if case_found:
            return self._format_case_answer(case_answer)
        
        # If only docs found
        if docs_found:
            return self._format_docs_answer(docs_answer)
        
        # Nothing found - check if it's a real question
        if "?" in question or len(question) > 50:
            return "INSUFFICIENT_INFO"
        
        return "SKIP"
    
    def _format_chat_first_answer(self, question: str, chat_answer: str, case_answer: str = None, docs_answer: str = None) -> str:
        """
        Format answer with chat as primary, enriched by cases/docs.
        """
        # For simple cases, just return the chat answer
        if not case_answer and not docs_answer:
            return chat_answer
        
        # Synthesize when we have multiple sources
        sources = f"**Chat History (Primary):**\n{chat_answer}\n\n"
        if case_answer:
            sources += f"**Similar Cases:**\n{case_answer}\n\n"
        if docs_answer:
            sources += f"**Documentation:**\n{docs_answer}\n\n"
        
        prompt = f"""You are a technical support assistant. Synthesize a final answer from multiple sources.

User Question: "{question}"

{sources}

RULES:
1. PRIORITIZE Chat History - it contains actual solutions from the community
2. Use Cases to confirm or add details
3. Use Docs for official specifications only
4. Keep citations from the sources
5. Be concise and helpful
6. Use the same language as the question

Final Answer:"""
        
        try:
            response = self.model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            # Fallback to chat answer
            return chat_answer
    
    def _format_case_answer(self, case_answer: str) -> str:
        """Format answer from cases only."""
        return case_answer
    
    def _format_docs_answer(self, docs_answer: str) -> str:
        """Format answer from docs only."""
        return docs_answer


# Backward compatibility alias
UltimateAgent = UltimateAgentV2


# =============================================================================
# MAIN
# =============================================================================
if __name__ == "__main__":
    agent = UltimateAgentV2()
    
    print("Ultimate Agent v2 Ready. Type 'exit' to quit.\n")
    while True:
        q = input("Q: ").strip()
        if q.lower() in ["exit", "quit", ""]:
            break
        
        answer = agent.answer(q)
        print(f"\nA: {answer}\n")
