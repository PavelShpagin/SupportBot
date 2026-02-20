import os

content = r'''import os
import sys
import json
import google.generativeai as genai
from pathlib import Path
from .gemini_agent import GeminiAgent, build_context_from_description
from .chat_search_agent import ChatSearchAgent
from .case_search_agent import CaseSearchAgent

# Fix encoding
sys.stdout.reconfigure(encoding='utf-8')

# Configuration
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    try:
        from dotenv import load_dotenv
        load_dotenv()
        GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
    except ImportError:
        pass

if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)

MODEL_NAME = "gemini-2.0-flash"

class UltimateAgent:
    def __init__(self):
        print("Initializing Ultimate Agent...")
        
        # Determine data directory relative to this file
        # This file is in app/agent/ultimate_agent.py
        # Data is in data/
        base_dir = Path(__file__).resolve().parent.parent.parent
        self.data_dir = base_dir / "data"
        
        # 1. Docs Agent (Official Rules)
        description_path = self.data_dir / "description.txt"
        if description_path.exists():
            context = build_context_from_description(str(description_path))
            self.docs_agent = GeminiAgent(context)
        else:
            print(f"Warning: Docs not found at {description_path}")
            self.docs_agent = None
            
        # 2. Chat Search Agent (Community Knowledge)
        index_path = self.data_dir / "chat_index.pkl"
        if index_path.exists():
            self.chat_agent = ChatSearchAgent(str(index_path))
        else:
            print(f"Warning: Chat index not found at {index_path}")
            self.chat_agent = None
            
        # 3. Case Search Agent (Structured Past Tickets)
        cases_path = self.data_dir / "signal_cases_structured.json"
        if cases_path.exists():
            self.case_agent = CaseSearchAgent(str(cases_path))
        else:
            print(f"Warning: Cases not found at {cases_path}")
            self.case_agent = None
            
        # 4. Synthesizer Model
        self.synthesizer = genai.GenerativeModel(MODEL_NAME)

    def answer(self, question):
        print(f"\n--- Ultimate Processing: {question} ---")
        
        # 1. Get Docs Answer (Fastest, Most Trusted)
        docs_ans = "Docs Agent not available."
        if self.docs_agent:
            try:
                docs_ans = self.docs_agent.answer(question)
            except Exception as e:
                docs_ans = f"Error: {e}"
        
        # If Docs said SKIP, we trust it and stop immediately to save latency/cost.
        if "SKIP" in docs_ans:
            return "SKIP"

        # 2. Get Case Answer
        case_ans = "Case Agent not available."
        if self.case_agent:
            try:
                case_ans = self.case_agent.answer(question)
            except Exception as e:
                case_ans = f"Error: {e}"

        # 3. Get Chat Answer
        chat_ans = "Chat Agent not available."
        if self.chat_agent:
            try:
                chat_ans = self.chat_agent.answer(question)
            except Exception as e:
                chat_ans = f"Error: {e}"
        
        # 4. Check for SKIP/INSUFFICIENT (Fast Fail)
        # We want to catch obvious failures early to avoid wasting LLM tokens on synthesis
        # But we also want to catch "soft" failures where the agent talks a lot but says nothing.
        
        fail_phrases = ["INSUFFICIENT_INFO", "SKIP", "No relevant", "I don't know", "cannot answer", "not found"]
        
        docs_failed = any(p in docs_ans for p in fail_phrases)
        case_failed = any(p in case_ans for p in fail_phrases)
        chat_failed = any(p in chat_ans for p in fail_phrases)
        
        # If all failed, Tag Admin immediately
        if docs_failed and case_failed and chat_failed:
            return "INSUFFICIENT_INFO"

        # 5. Synthesize & Decide
        prompt = f"""
You are a Senior Support Decision Maker. Your primary role is to decide if the retrieved information is sufficient to answer the user's request.

User Question: "{question}"

RETRIEVED INFORMATION:
1. **Docs Agent** (Official):
{docs_ans}

2. **Case Agent** (Past Tickets):
{case_ans}

3. **Chat Agent** (Discussions):
{chat_ans}

### DECISION PROCESS:
1. **ANALYZE**: Do the sources contain a **specific, actionable answer** to the User Question?
   - *Bad*: "I found some info about general settings..." (Too vague)
   - *Bad*: "There are no cases exactly matching..." (Irrelevant)
   - *Good*: "Change setting X to Y." (Specific)

2. **JUDGE**:
   - If the info is **vague, irrelevant, or missing** -> Output exactly: `INSUFFICIENT_INFO`
   - If the info is **sufficient** -> Proceed to synthesize the answer.

### SYNTHESIS RULES (Only if sufficient):
- **Concise**: Write short, direct paragraphs. No fluff.
- **Language**: Ukrainian.
- **Citations**: MANDATORY. Format: `... [Source: Docs/Case #123/Chat]`.
- **Tone**: Professional, helpful.

### OUTPUT:
Return ONLY the final answer text OR the string `INSUFFICIENT_INFO`.
"""
        response = self.synthesizer.generate_content(prompt)
        return response.text.strip()
'''

with open('signal-bot/app/agent/ultimate_agent.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("Updated ultimate_agent.py")
