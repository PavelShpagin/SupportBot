import os
import sys
import json
import time
import google.generativeai as genai
from pathlib import Path
from .gemini_agent import GeminiAgent, build_context_from_description, build_context_from_urls
from .chat_search_agent import ChatSearchAgent
from .case_search_agent import CaseSearchAgent
from app.config import load_settings
from app.db import get_group_docs
from app.rag.chroma import create_chroma
from app.llm.client import LLMClient

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
        
        self.settings = load_settings()
        self.public_url = self.settings.public_url.rstrip('/')
        
        # Determine data directory relative to this file
        # This file is in app/agent/ultimate_agent.py
        # Data is in data/
        base_dir = Path(__file__).resolve().parent.parent.parent
        self.data_dir = base_dir / "data"
        
        # Initialize agents
        self.docs_agent = None
        self.chat_agent = None
        self.case_agent = None
        self.synthesizer = None
        
        # Cache for group-specific docs agents
        # group_id -> (agent, load_time)
        self.group_agents = {}
        
        self.load_agents()

    def load_agents(self):
        """Loads or reloads all sub-agents."""
        print("Loading agents...", flush=True)
        
        # Clear group cache on reload
        self.group_agents = {}
        
        # 1. Docs Agent (Official Rules)
        description_path = self.data_dir / "description.txt"
        if description_path.exists():
            context = build_context_from_description(str(description_path))
            # Re-create GeminiAgent (this will create a new cache)
            self.docs_agent = GeminiAgent(context)
        else:
            print(f"Warning: Docs not found at {description_path}")
            self.docs_agent = None
            
        # 2. Chat Search Agent (Community Knowledge)
        index_path = self.data_dir / "chat_index.pkl"
        if index_path.exists():
            self.chat_agent = ChatSearchAgent(str(index_path), public_url=self.public_url)
        else:
            print(f"Warning: Chat index not found at {index_path}")
            self.chat_agent = None
            
        # 3. Case Search Agent (Structured Past Tickets)
        # We now use the live Chroma DB instead of static file
        self.rag = create_chroma(self.settings)
        self.llm = LLMClient(self.settings)
        self.case_agent = CaseSearchAgent(rag=self.rag, llm=self.llm, public_url=self.public_url)
            
        # 4. Synthesizer Model
        self.synthesizer = genai.GenerativeModel(MODEL_NAME)
        
        self.last_load_time = time.time()
        print("Agents loaded.", flush=True)

    def get_docs_agent(self, group_id, db):
        """Get the appropriate DocsAgent for the group."""
        if not group_id or not db:
            return self.docs_agent
            
        # Check cache
        if group_id in self.group_agents:
            agent, load_time = self.group_agents[group_id]
            # Cache for 10 minutes
            if time.time() - load_time < 600:
                return agent
                
        # Try to load from DB
        try:
            urls = get_group_docs(db, group_id)
            if urls:
                print(f"Loading specific docs for group {group_id}: {urls}", flush=True)
                context = build_context_from_urls(urls)
                agent = GeminiAgent(context)
                self.group_agents[group_id] = (agent, time.time())
                return agent
        except Exception as e:
            print(f"Error loading group docs for {group_id}: {e}", flush=True)
            
        # Fallback to default
        return self.docs_agent

    def answer(self, question, group_id=None, db=None):
        # Check if we need to refresh agents (e.g. every 10 minutes)
        # This allows updating docs without restarting the container.
        REFRESH_INTERVAL = 600  # 10 minutes
        if time.time() - self.last_load_time > REFRESH_INTERVAL:
            print("Refreshing agents due to timeout...", flush=True)
            try:
                self.load_agents()
            except Exception as e:
                print(f"Error refreshing agents: {e}", flush=True)
        
        print(f"\n--- Ultimate Processing: {question} (group={group_id}) ---")
        
        # 1. Get Docs Answer (Fastest, Most Trusted)
        docs_agent = self.get_docs_agent(group_id, db)
        docs_ans = "Docs Agent not available."
        
        if docs_agent:
            try:
                docs_ans = docs_agent.answer(question)
            except Exception as e:
                docs_ans = f"Error: {e}"
        
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
            # Instead of returning just "INSUFFICIENT_INFO", we return a tag trigger
            return "[[TAG_ADMIN]]"

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

DECISION PROTOCOL:
1. **EVALUATE**: Read the answers from Docs, Cases, and Chat.
2. **CHECK VALIDITY**: 
   - If the answers are "INSUFFICIENT_INFO", "No relevant cases", "No relevant discussions", or just generic/irrelevant info:
   - -> Output "[[TAG_ADMIN]]" ONLY. Do not add any text.
3. **SYNTHESIZE**:
   - If valid info exists, write a helpful response.
   - **PRIORITIZE OFFICIAL INFO**: Trust Docs > Cases > Chat.
   - **BE CONCISE**: Write short, direct paragraphs. No fluff. No bold/stars unless critical.
   - **CITATIONS**: You MUST keep the citations.
   - **FORMAT**: For cases, use ONLY the bracketed link: [http://...]. Do NOT add "Source:", "Case:", or IDs.
   - **LANGUAGE**: Ukrainian.

CRITICAL:
- If you cannot answer based *strictly* on the provided sources, output "[[TAG_ADMIN]]" ONLY.
- Do NOT apologize.
- Do NOT make up info.
- Do NOT use "Tip..." or "Загальна порада:".

Output ONLY the final response.
"""
        response = self.synthesizer.generate_content(prompt)
        return response.text.strip()
