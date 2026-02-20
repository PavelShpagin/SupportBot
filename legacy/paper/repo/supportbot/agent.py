import os
import google.generativeai as genai
from typing import List, Dict, Any, Optional
try:
    from .storage import SimpleVectorStore, LocalBlobStorage
except ImportError:
    from storage import SimpleVectorStore, LocalBlobStorage

# Configuration
MODEL_NAME = "gemini-2.0-flash"
EMBEDDING_MODEL = "models/gemini-embedding-001"

class SupportBot:
    def __init__(self, data_dir: str = "paper/repo/data"):
        self.data_dir = data_dir
        self.docs_store = SimpleVectorStore(os.path.join(data_dir, "docs/vector_store"))
        self.cases_store = SimpleVectorStore(os.path.join(data_dir, "cases/vector_store"))
        self.blob_storage = LocalBlobStorage(os.path.join(data_dir, "blobs"))
        
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            print("Warning: GOOGLE_API_KEY not set")
        else:
            genai.configure(api_key=api_key)
            self.model = genai.GenerativeModel(MODEL_NAME)

    def get_embedding(self, text: str) -> List[float]:
        try:
            result = genai.embed_content(
                model=EMBEDDING_MODEL,
                content=text,
                task_type="retrieval_query"
            )
            return result['embedding']
        except Exception as e:
            print(f"Embedding error: {e}")
            return []

    def gate(self, message: str) -> str:
        prompt = f"""
Classify the following message from a support chat into one of these categories:
- new_question: A new support query or problem.
- ongoing_discussion: A follow-up or continuation of an existing discussion.
- statement: A statement of fact, summary, or update that doesn't need a response.
- noise: Greetings, emoji-only, or off-topic.

Message: "{message}"

Category:
"""
        try:
            response = self.model.generate_content(prompt)
            return response.text.strip().lower()
        except Exception as e:
            print(f"Gating error: {e}")
            return "new_question"

    def retrieve_docs(self, query: str, k: int = 3) -> List[Dict[str, Any]]:
        embedding = self.get_embedding(query)
        if not embedding:
            return []
        
        results = self.docs_store.search(embedding, k=k)
        return results

    def retrieve_cases(self, query: str, k: int = 3) -> List[Dict[str, Any]]:
        embedding = self.get_embedding(query)
        if not embedding:
            return []
        
        results = self.cases_store.search(embedding, k=k)
        for res in results:
            if 'blob_id' in res:
                content = self.blob_storage.load(res['blob_id'])
                if content:
                    res['full_content'] = content
        return results

    def answer(self, query: str) -> Optional[str]:
        print(f"Processing: {query}")
        
        # 0. Gate
        category = self.gate(query)
        print(f"Category: {category}")
        
        if "noise" in category or "statement" in category:
            return None
        
        # 1. Retrieve Context
        docs = self.retrieve_docs(query)
        cases = self.retrieve_cases(query)
        
        # 2. Synthesize
        context_str = ""
        
        if docs:
            context_str += "OFFICIAL DOCUMENTATION:\n"
            for d in docs:
                context_str += f"- {d.get('title', 'Doc')}: {d.get('text', '')[:500]}...\n"
                if 'url' in d:
                    context_str += f"  URL: {d['url']}\n"
            context_str += "\n"
            
        if cases:
            context_str += "PAST SOLVED CASES:\n"
            for c in cases:
                context_str += f"- Case #{c.get('id', '?')}: {c.get('problem_summary', '')}\n"
                context_str += f"  Solution: {c.get('solution_summary', '')}\n"
                # Mock URL for the paper demo since we don't have a live webapp
                context_str += f"  URL: https://supportbot.info/case/{c.get('id', '?')}\n"
            context_str += "\n"

        prompt = f"""
You are an expert Support Engineer for Signal.
User Query: "{query}"

Context:
{context_str}

TASK:
Synthesize a final answer for the user.

DECISION PROTOCOL:
1. **EVALUATE**: Read the provided context (Docs and Past Cases).
2. **CHECK VALIDITY**: 
   - If the context is empty or irrelevant, output "I don't have enough information to answer this."
3. **SYNTHESIZE**:
   - If valid info exists, write a helpful response.
   - **PRIORITIZE OFFICIAL INFO**: Trust Docs > Past Cases.
   - **BE CONCISE**: Write short, direct paragraphs. No fluff.
   - **CITATIONS**: You MUST keep the citations (e.g. [Source: Docs...], [Case #123]).
   - **LINKS**: If a Case URL is provided, you MUST include it in the response as a reference.
   - **LANGUAGE**: English.

Output ONLY the final response.
"""
        try:
            response = self.model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            return f"Error generating answer: {e}"

if __name__ == "__main__":
    bot = SupportBot()
    while True:
        q = input("\nQ: ")
        if q.lower() in ["exit", "quit"]:
            break
        ans = bot.answer(q)
        if ans:
            print(f"A: {ans}")
        else:
            print("A: [No Response Triggered]")
