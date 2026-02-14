import os
import sys
import json
import google.generativeai as genai
from gemini_agent import GeminiAgent, build_context_from_description
from chat_search_agent import ChatSearchAgent
from case_search_agent import CaseSearchAgent

# Fix encoding
sys.stdout.reconfigure(encoding='utf-8')

# Configuration
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    raise ValueError("GOOGLE_API_KEY not set")
genai.configure(api_key=GOOGLE_API_KEY)

MODEL_NAME = "gemini-2.0-flash"

class UltimateAgent:
    def __init__(self):
        print("Initializing Ultimate Agent...")
        
        # 1. Docs Agent (Official Rules)
        # Try paper/repo/docs.txt first (Signal specific), then fallback to test/description.txt
        description_path = "paper/repo/docs.txt"
        if not os.path.exists(description_path):
            description_path = "test/description.txt"
            
        if os.path.exists(description_path):
            context = build_context_from_description(description_path)
            self.docs_agent = GeminiAgent(context)
        else:
            print("Warning: Docs not found.")
            self.docs_agent = None
            
        # 2. Chat Search Agent (Community Knowledge)
        index_path = "test/data/chat_index.pkl"
        if os.path.exists(index_path):
            self.chat_agent = ChatSearchAgent(index_path)
        else:
            print("Warning: Chat index not found.")
            self.chat_agent = None
            
        # 3. Case Search Agent (Structured Past Tickets)
        cases_path = "test/data/signal_cases_structured.json"
        if os.path.exists(cases_path):
            self.case_agent = CaseSearchAgent(cases_path)
        else:
            print("Warning: Cases not found.")
            self.case_agent = None
            
        # 4. Synthesizer Model
        self.synthesizer = genai.GenerativeModel(MODEL_NAME)

    def answer(self, question, group_id=None):
        print(f"\n--- Ultimate Processing: {question} (group={group_id}) ---")
        
        # 1. Get Docs Answer (Fastest, Most Trusted)
        docs_ans = "Docs Agent not available."
        if self.docs_agent:
            try:
                docs_ans = self.docs_agent.answer(question)
            except Exception as e:
                docs_ans = f"Error: {e}"
        # print(f"Docs Answer: {docs_ans[:100]}...")

        # Optimization: If Docs Agent confidently answers (not SKIP/INSUFFICIENT), we might stop here?
        # BUT: Sometimes Cases/Chat have *better* practical details. 
        # Let's run all for the "Ultimate" version to ensure max quality.
        # We can optimize later.
        
        # If Docs said SKIP, we trust it and stop immediately to save latency/cost.
        if "SKIP" in docs_ans:
            return "SKIP"

        # 2. Get Case Answer
        case_ans = "Case Agent not available."
        if self.case_agent:
            try:
                case_ans = self.case_agent.answer(question, group_id=group_id)
            except Exception as e:
                case_ans = f"Error: {e}"
        # print(f"Case Answer: {case_ans[:100]}...")

        # 3. Get Chat Answer
        chat_ans = "Chat Agent not available."
        if self.chat_agent:
            try:
                chat_ans = self.chat_agent.answer(question)
            except Exception as e:
                chat_ans = f"Error: {e}"
        # print(f"Chat Answer: {chat_ans[:100]}...")
        
        # 4. Check for SKIP/INSUFFICIENT
        docs_failed = "INSUFFICIENT_INFO" in docs_ans or "SKIP" in docs_ans
        # Chat/Case usually return text "No relevant...", treat as failed if so.
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
6. **TONE**: Helpful, technical, concise. English language.

Output ONLY the final response.
"""
        response = self.synthesizer.generate_content(prompt)
        return response.text.strip()

if __name__ == "__main__":
    agent = UltimateAgent()
    while True:
        q = input("\nQ: ")
        if q.lower() in ["exit", "quit"]:
            break
        print(f"A: {agent.answer(q)}")
