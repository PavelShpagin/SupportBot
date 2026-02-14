import os
import sys
import re
import pickle
import numpy as np
import google.generativeai as genai
import datetime

# Fix encoding
sys.stdout.reconfigure(encoding='utf-8')

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
    def __init__(self, index_path, public_url="https://supportbot.info"):
        self.tool = ChatSearchTool(index_path)
        self.public_url = public_url
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

    def answer(self, question, return_details=False):
        # Step 1: Ask LLM what to search
        prompt1 = f"""
User Question: "{question}"

I need to search the chat history to find an answer.
What search query should I use? 
Output ONLY the search query.
"""
        response1 = self.model.generate_content(prompt1)
        query = response1.text.strip()
        
        # Step 2: Search
        results = self.tool.search(query, k=5)
        
        if not results:
            if return_details:
                return {"answer": "No relevant discussions found in chat history.", "context": []}
            return "No relevant discussions found in chat history."
            
        # Step 3: Format results for LLM to decide if it needs context
        results_text = "\n".join([
            f"ID: {r['id']}\nDate: {r['date']}\nSender: {r['sender']}\nText: {r['text']}\nLink: {self.public_url}/chat/{r['id']}\n---"
            for r in results
        ])
        
        # Step 4: Ask LLM if it needs context or can answer
        prompt2 = f"""
User Question: "{question}"
Search Query: "{query}"

Search Results:
{results_text}

Do you have enough info to answer? 
If YES, provide the answer with citations. Each citation MUST include the link provided in the search results (e.g. [Source: Sender, Date, Link]).
If NO, and you need to see the conversation context for a specific message, output "CONTEXT: <message_id>".
"""
        response2 = self.model.generate_content(prompt2)
        text2 = response2.text.strip()
        
        final_answer = text2
        final_context = results_text
        
        if "CONTEXT:" in text2:
            # Extract ID
            match = re.search(r"CONTEXT: (\S+)", text2)
            if match:
                msg_id = match.group(1)
                context = self.tool.get_context(msg_id)
                final_context = context # Update context to the zoomed-in version
                
                # Final Answer
                prompt3 = f"""
User Question: "{question}"
Context for message {msg_id}:
{context}

Based on this discussion, answer the question. Cite the participants.
"""
                response3 = self.model.generate_content(prompt3)
                final_answer = response3.text
        
        if return_details:
            return {"answer": final_answer, "context": final_context}
        return final_answer
