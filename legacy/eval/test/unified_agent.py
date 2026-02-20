import os
import sys
import json
import google.generativeai as genai
from gemini_agent import GeminiAgent, build_context_from_description
from chat_search_agent import ChatSearchAgent

# Fix encoding
sys.stdout.reconfigure(encoding='utf-8')

# Configuration
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    raise ValueError("GOOGLE_API_KEY not set")
genai.configure(api_key=GOOGLE_API_KEY)

MODEL_NAME = "gemini-2.0-flash"

class UnifiedAgent:
    def __init__(self):
        print("Initializing Unified Agent...")
        
        # 1. Docs Agent
        description_path = "test/description.txt"
        if os.path.exists(description_path):
            context = build_context_from_description(description_path)
            self.docs_agent = GeminiAgent(context)
        else:
            print("Warning: Docs not found.")
            self.docs_agent = None
            
        # 2. Chat Search Agent
        index_path = "test/data/chat_index.pkl"
        if os.path.exists(index_path):
            self.chat_agent = ChatSearchAgent(index_path)
        else:
            print("Warning: Chat index not found.")
            self.chat_agent = None
            
        # 3. Synthesizer Model
        self.synthesizer = genai.GenerativeModel(MODEL_NAME)

    def answer(self, question):
        print(f"\n--- Unified Processing: {question} ---")
        
        # 1. Get Docs Answer
        docs_ans = "Docs Agent not available."
        if self.docs_agent:
            try:
                docs_ans = self.docs_agent.answer(question)
            except Exception as e:
                docs_ans = f"Error: {e}"
        print(f"Docs Answer: {docs_ans[:100]}...")

        # 2. Get Chat Answer
        chat_ans = "Chat Agent not available."
        if self.chat_agent:
            try:
                chat_ans = self.chat_agent.answer(question)
            except Exception as e:
                chat_ans = f"Error: {e}"
        print(f"Chat Answer: {chat_ans[:100]}...")
        
        # 3. Check for "SKIP" or "INSUFFICIENT_INFO"
        docs_failed = "INSUFFICIENT_INFO" in docs_ans or "SKIP" in docs_ans
        chat_failed = "No relevant discussions" in chat_ans or "SKIP" in chat_ans # Chat agent usually returns text
        
        # If both failed/skipped, return the Docs failure (which tags admin or skips)
        if docs_failed and chat_failed:
            if "SKIP" in docs_ans:
                return "SKIP"
            return "INSUFFICIENT_INFO"

        # 4. Synthesize
        # If Docs succeeded, it's the authority.
        # If Chat succeeded, it's helpful context.
        
        prompt = f"""
You are a Senior Support Engineer. You have received answers from two junior agents:
1. Documentation Agent (Official Source)
2. Chat History Agent (Community Knowledge)

User Question: "{question}"

Docs Agent Answer:
{docs_ans}

Chat Agent Answer:
{chat_ans}

TASK:
Synthesize a final answer for the user.
- **PRIORITIZE** the Documentation Agent. If it gave a clear answer with citations, use it as the core.
- **AUGMENT** with the Chat Agent's answer ONLY if it adds useful details, troubleshooting steps, or community confirmation that isn't in the docs.
- **IGNORE** the Chat Agent if it contradicts the Docs or seems irrelevant.
- **CITE SOURCES**: Keep the citations from the original answers (e.g. [Source: URL] or [Date, Sender]).
- If the Docs Agent said "INSUFFICIENT_INFO" but the Chat Agent found a solution, use the Chat Agent's answer but add a disclaimer: "Note: This is based on community discussions, not official docs."
- If both failed, output "INSUFFICIENT_INFO".

Output ONLY the final response.
"""
        response = self.synthesizer.generate_content(prompt)
        return response.text.strip()

if __name__ == "__main__":
    agent = UnifiedAgent()
    while True:
        q = input("\nQ: ")
        if q.lower() in ["exit", "quit"]:
            break
        print(f"A: {agent.answer(q)}")
