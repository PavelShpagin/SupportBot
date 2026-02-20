import os
import sys
import json
import google.generativeai as genai
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.rag.chroma import ChromaRag
    from app.llm.client import LLMClient
    from app.config import Settings

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
    def __init__(self, rag: Optional['ChromaRag'] = None, llm: Optional['LLMClient'] = None, settings: Optional['Settings'] = None):
        print("Initializing Ultimate Agent...")
        
        self.rag = rag
        self.llm = llm
        self.settings = settings
        
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

    def answer(self, question, group_id: Optional[str] = None):
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
        
        # Prefer Live RAG if available
        if self.rag and self.llm and group_id:
            try:
                print(f"DEBUG: Searching Live RAG for group_id={group_id}")
                emb = self.llm.embed(text=question)
                results = self.rag.retrieve_cases(group_id=group_id, embedding=emb, k=3)
                
                if not results:
                    case_ans = "No relevant cases found in live database."
                else:
                    case_ans = "Found similar past cases:\n"
                    public_url = self.settings.public_url if self.settings else "https://supportbot.info"
                    public_url = public_url.rstrip("/")
                    
                    for r in results:
                        case_id = r['case_id']
                        url = f"{public_url}/case/{case_id}"
                        doc_text = r.get('document', '')
                        # Add URL to the case description for the synthesizer to see
                        case_ans += f"- Case {url}:\n{doc_text}\n\n"
            except Exception as e:
                print(f"Error querying live RAG: {e}")
                case_ans = f"Error querying live RAG: {e}"
        
        # Fallback to static file if RAG not available or failed (or empty results? maybe not fallback if empty)
        elif self.case_agent:
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
   - -> You MUST decide:
     - Option A: If you have NO idea -> Output "[[TAG_ADMIN]]"
     - Option B: If you have a general tip -> Write a SUPER SHORT sentence (max 10 words) and append " @admin, підкажеш?"
3. **SYNTHESIZE**:
   - If valid info exists, write a helpful response.
   - **PRIORITIZE OFFICIAL INFO**: Trust Docs > Cases > Chat.
   - **BE CONCISE**: Write short, direct paragraphs. No fluff. No bold/stars unless critical.
   - **CITATIONS**: You MUST keep the citations (e.g. [Source: Docs...]).
   - **LINKS**: If a Case URL is provided (https://...), you MUST include it in the response as a reference.
   - **LANGUAGE**: Ukrainian.

CRITICAL:
- If you cannot answer based *strictly* on the provided sources, append " @admin, підкажеш?" to your response.
- Do NOT apologize.
- Do NOT make up info.
- Do NOT use "Tip..." or "Загальна порада:". Just the advice.

Output ONLY the final response.
"""
        response = self.synthesizer.generate_content(prompt)
        return response.text.strip()
