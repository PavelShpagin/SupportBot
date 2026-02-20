import os
from pathlib import Path

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
        
        # 4. Check for SKIP/INSUFFICIENT
        docs_failed = "INSUFFICIENT_INFO" in docs_ans or "SKIP" in docs_ans
        case_failed = "No relevant cases" in case_ans or "No relevant" in case_ans
        chat_failed = "No relevant discussions" in chat_ans or "SKIP" in chat_ans
        
        # If all failed, Tag Admin
        if docs_failed and case_failed and chat_failed:
            return "INSUFFICIENT_INFO" # Will be converted to Tag Admin by Gate

        # 5. Synthesize
        prompt = f"""
You are a Senior Support Engineer. You have received answers from three sources:
1. **Documentation** (Official Source - Highest Priority)
2. **Past Cases** (Solved Tickets - High Priority)
3. **Chat History** (Community Discussion - Medium Priority)

User Question: "{question}"

Docs Agent Answer:
{docs_ans}

Case Agent Answer:
{case_ans}

Chat Agent Answer:
{chat_ans}

TASK:
Synthesize a final answer for the user.

RULES:
1. **PRIORITIZE OFFICIAL INFO**: If Docs Agent gives a clear answer, use it as the core.
2. **ENRICH WITH CASES**: If Past Cases provide a specific solution to a problem not fully covered in docs, ADD it.
3. **USE CHAT CAUTIOUSLY**: Use Chat History only if it adds unique value (e.g. specific hardware compatibility confirmed by users).
4. **CONFLICT RESOLUTION**: If sources contradict, trust Docs > Cases > Chat.
5. **CITATIONS**: You MUST keep the citations from the original answers.
   - Format: "Answer... [Source: Docs/Case #123/Chat Date]"
6. **TONE**: Helpful, technical, concise. Ukrainian language.

Output ONLY the final response.
"""
        response = self.synthesizer.generate_content(prompt)
        return response.text.strip()
'''

with open("signal-bot/app/agent/ultimate_agent.py", "w", encoding="utf-8") as f:
    f.write(content)
print("File written successfully.")
