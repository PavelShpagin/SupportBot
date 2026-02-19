import os
import sys
import pickle
import json
import numpy as np
import google.generativeai as genai
from google.generativeai import caching
import datetime
from pathlib import Path

# Fix encoding
sys.stdout.reconfigure(encoding='utf-8')

def _maybe_load_dotenv(dotenv_path):
    path = Path(dotenv_path)
    if not path.exists():
        return
    print(f"Loading .env from {path}")
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip("\r")
        if not k:
            continue
        if (v.startswith("'") and v.endswith("'")) or (v.startswith('"') and v.endswith('"')):
            v = v[1:-1]
        os.environ.setdefault(k, v)

_maybe_load_dotenv(".env")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    raise ValueError("GOOGLE_API_KEY not set")
genai.configure(api_key=GOOGLE_API_KEY)

MODEL_NAME = "gemini-2.0-flash"
EMBEDDING_MODEL = "models/gemini-embedding-001"

class ChatSearchTool:
    def __init__(self, index_path):
        self.index_path = index_path
        self.messages = []
        self.embeddings = []
        self._load_index()

    def _load_index(self):
        print(f"Loading chat index from {self.index_path}...")
        with open(self.index_path, "rb") as f:
            self.messages = pickle.load(f)
        
        # Extract embeddings to numpy array
        self.embeddings = np.array([m["embedding"] for m in self.messages])
        print(f"Loaded {len(self.messages)} messages.")

    def search(self, query, k=5):
        """Searches chat history for relevant messages."""
        print(f"DEBUG: Searching for '{query}'...")
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
            
            # Top K
            top_indices = np.argsort(scores)[-k:][::-1]
            
            results = []
            for idx in top_indices:
                msg = self.messages[idx]
                results.append({
                    "id": msg["id"],
                    "score": float(scores[idx]),
                    "date": datetime.datetime.fromtimestamp(msg["timestamp"]/1000).strftime('%Y-%m-%d %H:%M'),
                    "sender": msg["sender"],
                    "text": msg["text"]
                })
            return results
        except Exception as e:
            print(f"Search error: {e}")
            return []

    def get_context(self, message_id, radius=3):
        """Gets surrounding messages for context."""
        print(f"DEBUG: Getting context for {message_id}...")
        target_idx = -1
        for i, m in enumerate(self.messages):
            if m["id"] == message_id:
                target_idx = i
                break
        
        if target_idx == -1:
            return "Message not found."
            
        start = max(0, target_idx - radius)
        end = min(len(self.messages), target_idx + radius + 1)
        
        context_msgs = self.messages[start:end]
        formatted = []
        for m in context_msgs:
            marker = ">>>" if m["id"] == message_id else "   "
            date = datetime.datetime.fromtimestamp(m["timestamp"]/1000).strftime('%H:%M')
            formatted.append(f"{marker} [{date}] {m['sender']}: {m['text']}")
            
        return "\n".join(formatted)

class ChatSearchAgent:
    def __init__(self, index_path, public_url=None):
        self.tool = ChatSearchTool(index_path)
        self.public_url = public_url or "http://localhost:3000"  # Default for citations
        self.model = genai.GenerativeModel(MODEL_NAME)
        self.chat = self.model.start_chat(history=[])
        
        # System prompt simulation (since start_chat doesn't always take system prompt in all SDK versions cleanly, 
        # we'll prepend it to the first message or use a wrapper).
        # Actually, 2.0 Flash supports system_instruction in constructor.
        self.model = genai.GenerativeModel(
            MODEL_NAME,
            system_instruction="""
You are a Support Assistant with access to historical chat logs.
Your goal is to answer user questions by finding similar past discussions.

TOOLS AVAILABLE:
1. SEARCH: You can search the chat history.
2. CONTEXT: You can zoom in on a message to see the conversation around it.

PROCESS:
1. Analyze the user's question.
2. Generate a search query to find similar past issues.
3. SEARCH the chat history.
4. If you find a relevant message but need to understand the solution, use CONTEXT to read the discussion.
5. Synthesize an answer.
   - You MUST cite the source message (Date, Sender).
   - If the chat history contains a solution, present it.
   - If the chat history is inconclusive or contradictory, warn the user.
   - If no relevant info is found, say "No relevant discussions found in history."

Be helpful, concise, and accurate.
"""
        )
        self.chat = self.model.start_chat(enable_automatic_function_calling=True)
        
        # Inject tools? 
        # The python SDK for automatic function calling is a bit different.
        # Let's use manual tool calling pattern for maximum control and robustness in this prototype.
        # Or better: Just use a simple ReAct loop or "Thought/Action" loop.
        # Given the simplicity, a simple "Search -> Answer" loop might suffice, but "Context" is key.
        # Let's implement a simple 2-step loop.

    def answer(self, question, return_details=False, html_citations=True):
        """
        Answer a question by searching chat history.
        
        Args:
            question: User question
            return_details: If True, return dict with answer and context
            html_citations: If True, format citations as HTML links
        """
        import re
        
        # Step 1: Search directly with the question (more effective than LLM-generated query)
        results = self.tool.search(question, k=5)
        
        # Filter by relevance score
        relevant = [r for r in results if r['score'] >= 0.5]
        
        if not relevant:
            if return_details:
                return {"answer": "No relevant discussions found in chat history.", "context": []}
            return "No relevant discussions found in chat history."
        
        # Step 2: Get context for top result if helpful
        top_result = relevant[0]
        context = self.tool.get_context(top_result['id'], radius=3)
        
        # Step 3: Build context from all relevant results
        context_parts = []
        for r in relevant[:3]:  # Top 3
            msg_url = f"{self.public_url}/message/{r['id']}"
            context_parts.append(f"[{r['date']}] {r['sender']}: {r['text']}")
        
        context_text = "\n".join(context_parts)
        
        # Step 4: Ask LLM to synthesize answer with citations
        prompt = f"""You are a technical support assistant. Answer the user's question based on relevant chat history.

User Question: "{question}"

Relevant Messages from Chat History:
{context_text}

Surrounding Context for Top Match:
{context}

RULES:
1. Answer directly based on the chat history
2. Include citations with date and sender: [YYYY-MM-DD, Sender Name]
3. If chat history contains the answer, provide it
4. If chat history is inconclusive, say so
5. Keep the same language as the question

Answer:"""
        
        try:
            response = self.model.generate_content(prompt)
            answer_text = response.text.strip()
            
            # Add HTML citations if requested
            if html_citations and answer_text:
                # Append source links
                citations_html = "\n\n<b>Sources:</b>\n"
                for r in relevant[:3]:
                    msg_url = f"{self.public_url}/message/{r['id']}"
                    citations_html += f'• <a href="{msg_url}">[{r["date"]}] {r["sender"]}</a>\n'
                answer_text += citations_html
            
            if return_details:
                return {"answer": answer_text, "context": context_text, "results": relevant}
            return answer_text
            
        except Exception as e:
            error_msg = f"Error generating answer: {e}"
            if return_details:
                return {"answer": error_msg, "context": context_text}
            return error_msg

# Simple runner
if __name__ == "__main__":
    import re
    agent = ChatSearchAgent("test/data/chat_index.pkl")
    
    while True:
        q = input("\nQ: ")
        if q.lower() in ["exit", "quit"]:
            break
        print(f"A: {agent.answer(q)}")
