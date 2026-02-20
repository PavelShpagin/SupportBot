"""
SupportBot Clean Agent

Architecture:
    Message → Gate → [Docs | Cases | Chat] → Aggregator → Response

Components:
    1. Gate: Classifies intent (SUPPORT_REQUEST | NOISE | AMBIGUOUS)
    2. Docs Tool: Official documentation (Google Docs, cached)
    3. Cases Tool: Solved tickets RAG (embeddings)
    4. Chat Tool: Message history RAG (embeddings)
    5. Aggregator: Synthesizes answer with citations or tags admin

Output:
    - SKIP: Message is noise (greeting, thanks, off-topic)
    - ANSWER: Synthesized response with citations
    - TAG_ADMIN: Genuine question but insufficient info
"""

import os
import sys
import json
import pickle
import numpy as np
import google.generativeai as genai
from pathlib import Path
from datetime import datetime

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
EMBEDDING_MODEL = "models/gemini-embedding-001"


# =============================================================================
# GATE: Classify message intent
# =============================================================================
class Gate:
    """
    Classifies whether a message is a support request.
    
    Returns:
        - SUPPORT: Technical question or help request
        - NOISE: Greeting, thanks, off-topic, reaction
        - AMBIGUOUS: Unclear intent, might need context
    """
    
    def __init__(self):
        self.model = genai.GenerativeModel(MODEL)
        self.prompt = """Classify this message from a technical support chat.

Message: "{message}"

Categories:
- SUPPORT: Technical question, problem report, feature request, "how to" question
- NOISE: Greeting ("hi", "hello"), thanks ("дякую", "thanks"), reaction ("+", "👍"), off-topic chat
- AMBIGUOUS: Could be support-related but unclear without context

Rules:
1. Short messages (<5 words) with no technical terms = likely NOISE
2. Messages with question marks, error codes, technical terms = SUPPORT
3. Messages with URLs, attachments mentioned = SUPPORT (sharing info for help)
4. Follow-up statements ("it works now", "still broken") = SUPPORT

Output ONLY one word: SUPPORT, NOISE, or AMBIGUOUS"""

    def classify(self, message: str) -> str:
        """Returns SUPPORT, NOISE, or AMBIGUOUS"""
        try:
            response = self.model.generate_content(
                self.prompt.format(message=message[:500])
            )
            result = response.text.strip().upper()
            if result in ["SUPPORT", "NOISE", "AMBIGUOUS"]:
                return result
            return "SUPPORT"  # Default to processing
        except Exception as e:
            print(f"Gate error: {e}")
            return "SUPPORT"  # Default to processing on error


# =============================================================================
# TOOLS: Retrieval from different knowledge sources
# =============================================================================
class DocsTool:
    """
    Searches official documentation.
    Uses Gemini context caching for efficiency.
    """
    
    def __init__(self, docs_path: str):
        self.context = self._load_docs(docs_path)
        self.model = genai.GenerativeModel(
            MODEL,
            system_instruction=f"""You are a documentation assistant.
Answer ONLY from the provided context. If the answer is not in the context, say "NOT_FOUND".
Always cite the source URL.

CONTEXT:
{self.context}"""
        )
    
    def _load_docs(self, path: str) -> str:
        """Load documentation from description file with URLs."""
        if not os.path.exists(path):
            return ""
        
        import requests
        import re
        
        context_parts = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                url_match = re.search(r"(https?://\S+)", line)
                if url_match:
                    url = url_match.group(1)
                    desc = line.replace(url, "").strip()
                    try:
                        # Fetch content
                        if "docs.google.com" in url:
                            doc_id = re.search(r"/document/d/([a-zA-Z0-9-_]+)", url)
                            if doc_id:
                                export_url = f"https://docs.google.com/document/d/{doc_id.group(1)}/export?format=txt"
                                content = requests.get(export_url, timeout=10).text
                        else:
                            content = requests.get(url, timeout=10).text[:10000]
                        context_parts.append(f"--- {desc} ({url}) ---\n{content[:5000]}")
                    except:
                        pass
        return "\n\n".join(context_parts)
    
    def search(self, query: str) -> dict:
        """Search docs for answer."""
        try:
            response = self.model.generate_content(query)
            text = response.text.strip()
            if "NOT_FOUND" in text:
                return {"found": False, "answer": None}
            return {"found": True, "answer": text}
        except Exception as e:
            return {"found": False, "answer": None, "error": str(e)}


class CasesTool:
    """
    RAG over solved support cases.
    Uses embeddings for semantic search.
    """
    
    def __init__(self, cases_path: str):
        self.cases = []
        self.embeddings = None
        self._load_cases(cases_path)
    
    def _load_cases(self, path: str):
        if not os.path.exists(path):
            print(f"Cases file not found: {path}")
            return
        
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        self.cases = data if isinstance(data, list) else data.get("cases", [])
        
        # Load or compute embeddings
        if self.cases and "embedding" in self.cases[0]:
            self.embeddings = np.array([c["embedding"] for c in self.cases])
        print(f"Loaded {len(self.cases)} cases.")
    
    def search(self, query: str, k: int = 3) -> dict:
        """Search for similar cases."""
        if not self.cases or self.embeddings is None:
            return {"found": False, "cases": []}
        
        try:
            # Embed query
            result = genai.embed_content(
                model=EMBEDDING_MODEL,
                content=query,
                task_type="retrieval_query"
            )
            query_emb = np.array(result['embedding'])
            
            # Cosine similarity
            scores = np.dot(self.embeddings, query_emb)
            top_idx = np.argsort(scores)[-k:][::-1]
            
            results = []
            for idx in top_idx:
                case = self.cases[idx]
                if scores[idx] > 0.5:  # Relevance threshold
                    results.append({
                        "score": float(scores[idx]),
                        "problem": case.get("problem", "")[:200],
                        "solution": case.get("solution", "")[:500],
                        "url": case.get("url", "")
                    })
            
            return {"found": len(results) > 0, "cases": results}
        except Exception as e:
            return {"found": False, "cases": [], "error": str(e)}


class ChatTool:
    """
    RAG over chat message history.
    Uses embeddings for semantic search.
    """
    
    def __init__(self, index_path: str):
        self.messages = []
        self.embeddings = None
        self._load_index(index_path)
    
    def _load_index(self, path: str):
        if not os.path.exists(path):
            print(f"Chat index not found: {path}")
            return
        
        with open(path, "rb") as f:
            self.messages = pickle.load(f)
        
        self.embeddings = np.array([m["embedding"] for m in self.messages])
        print(f"Loaded {len(self.messages)} messages.")
    
    def search(self, query: str, k: int = 5) -> dict:
        """Search chat history."""
        if not self.messages or self.embeddings is None:
            return {"found": False, "messages": []}
        
        try:
            # Embed query
            result = genai.embed_content(
                model=EMBEDDING_MODEL,
                content=query,
                task_type="retrieval_query"
            )
            query_emb = np.array(result['embedding'])
            
            # Cosine similarity
            scores = np.dot(self.embeddings, query_emb)
            top_idx = np.argsort(scores)[-k:][::-1]
            
            results = []
            for idx in top_idx:
                msg = self.messages[idx]
                if scores[idx] > 0.5:  # Relevance threshold
                    results.append({
                        "score": float(scores[idx]),
                        "date": datetime.fromtimestamp(msg["timestamp"]/1000).strftime('%Y-%m-%d'),
                        "text": msg["text"][:300],
                        "id": msg["id"]
                    })
            
            return {"found": len(results) > 0, "messages": results}
        except Exception as e:
            return {"found": False, "messages": [], "error": str(e)}


# =============================================================================
# AGGREGATOR: Synthesize response from multiple sources
# =============================================================================
class Aggregator:
    """
    Synthesizes final response from tool outputs.
    Decides: ANSWER (with citations) or TAG_ADMIN.
    """
    
    def __init__(self):
        self.model = genai.GenerativeModel(MODEL)
    
    def synthesize(self, question: str, docs_result: dict, cases_result: dict, chat_result: dict) -> dict:
        """
        Synthesize final response.
        
        Returns:
            {"action": "ANSWER"|"TAG_ADMIN", "response": str}
        """
        # Build context from available sources
        context_parts = []
        
        if docs_result.get("found"):
            context_parts.append(f"**Documentation:**\n{docs_result['answer']}")
        
        if cases_result.get("found"):
            cases_text = []
            for c in cases_result["cases"][:3]:
                cases_text.append(f"- Problem: {c['problem']}\n  Solution: {c['solution']}\n  [Source: {c.get('url', 'case history')}]")
            if cases_text:
                context_parts.append(f"**Similar Cases:**\n" + "\n".join(cases_text))
        
        if chat_result.get("found"):
            chat_text = []
            for m in chat_result["messages"][:5]:
                chat_text.append(f"- ({m['date']}) {m['text']}")
            if chat_text:
                context_parts.append(f"**Community Discussions:**\n" + "\n".join(chat_text))
        
        # If no relevant info found, tag admin
        if not context_parts:
            return {
                "action": "TAG_ADMIN",
                "response": "I couldn't find relevant information in documentation, cases, or chat history."
            }
        
        # Synthesize response
        prompt = f"""You are a technical support assistant. Answer the user's question using ONLY the provided context.

**User Question:** {question}

**Available Information:**
{chr(10).join(context_parts)}

**Rules:**
1. Answer directly and concisely
2. Cite sources using [Source: ...] format
3. If context is insufficient for a complete answer, say what you found and suggest asking an admin
4. Use the same language as the user's question

**Response:**"""
        
        try:
            response = self.model.generate_content(prompt)
            return {
                "action": "ANSWER",
                "response": response.text.strip()
            }
        except Exception as e:
            return {
                "action": "TAG_ADMIN",
                "response": f"Error generating response: {e}"
            }


# =============================================================================
# AGENT: Main orchestrator
# =============================================================================
class CleanAgent:
    """
    SupportBot Clean Agent
    
    Pipeline: Gate → [Docs | Cases | Chat] → Aggregator
    """
    
    def __init__(self, 
                 docs_path: str = "test/description.txt",
                 cases_path: str = "test/data/signal_cases_structured.json",
                 chat_path: str = "test/data/chat_index.pkl"):
        
        print("Initializing Clean Agent...")
        
        # Gate
        self.gate = Gate()
        
        # Tools
        self.docs = DocsTool(docs_path)
        self.cases = CasesTool(cases_path)
        self.chat = ChatTool(chat_path)
        
        # Aggregator
        self.aggregator = Aggregator()
        
        print("Agent ready.")
    
    def process(self, message: str, group_id: str = None) -> dict:
        """
        Process a message through the pipeline.
        
        Returns:
            {
                "action": "SKIP" | "ANSWER" | "TAG_ADMIN",
                "response": str or None,
                "gate_result": str,
                "sources_used": list
            }
        """
        # Step 1: Gate
        intent = self.gate.classify(message)
        
        if intent == "NOISE":
            return {
                "action": "SKIP",
                "response": None,
                "gate_result": "NOISE",
                "sources_used": []
            }
        
        # Step 2: Parallel retrieval (conceptually - in practice sequential for simplicity)
        docs_result = self.docs.search(message)
        cases_result = self.cases.search(message)
        chat_result = self.chat.search(message)
        
        sources = []
        if docs_result.get("found"): sources.append("docs")
        if cases_result.get("found"): sources.append("cases")
        if chat_result.get("found"): sources.append("chat")
        
        # Step 3: Aggregate
        result = self.aggregator.synthesize(message, docs_result, cases_result, chat_result)
        
        return {
            "action": result["action"],
            "response": result["response"],
            "gate_result": intent,
            "sources_used": sources
        }
    
    def answer(self, question: str, group_id: str = None, db=None) -> str:
        """Compatibility method for existing eval scripts."""
        result = self.process(question, group_id)
        
        if result["action"] == "SKIP":
            return "SKIP"
        elif result["action"] == "TAG_ADMIN":
            return "INSUFFICIENT_INFO"
        else:
            return result["response"]


# =============================================================================
# MAIN
# =============================================================================
if __name__ == "__main__":
    agent = CleanAgent()
    
    # Interactive mode
    print("\nClean Agent Ready. Type 'exit' to quit.\n")
    while True:
        q = input("Q: ").strip()
        if q.lower() in ["exit", "quit", ""]:
            break
        
        result = agent.process(q)
        print(f"\nAction: {result['action']}")
        print(f"Gate: {result['gate_result']}")
        print(f"Sources: {result['sources_used']}")
        if result['response']:
            print(f"Response: {result['response'][:500]}...")
        print()
