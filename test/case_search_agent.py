import json
import pickle
import numpy as np
import google.generativeai as genai
from pathlib import Path

# Fix encoding
import sys
sys.stdout.reconfigure(encoding='utf-8')

EMBEDDING_MODEL = "models/gemini-embedding-001"

class CaseSearchAgent:
    def __init__(self, cases_path):
        self.cases_path = cases_path
        self.cases = []
        self.embeddings = []
        self._load_cases()

    def _load_cases(self):
        print(f"Loading cases from {self.cases_path}...")
        with open(self.cases_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        self.cases = data.get("cases", [])
        print(f"Loaded {len(self.cases)} cases.")
        
        # Check if embeddings exist
        if self.cases and "embedding" in self.cases[0]:
            print("Embeddings found in file.")
            self.embeddings = np.array([c["embedding"] for c in self.cases])
        else:
            print("Embeddings NOT found. Please run build_case_index.py first (or use a file with embeddings).")
            # For now, we assume the file has embeddings as seen in the read_file output
            
    def search(self, query, k=3):
        """Searches cases for relevant problems/solutions."""
        print(f"DEBUG: Searching Cases for '{query}'...")
        try:
            # Embed query
            # We need to import os and configure genai here if not done globally, 
            # but usually it's done by the caller. 
            # For safety, let's assume caller configured it.
            
            result = genai.embed_content(
                model=EMBEDDING_MODEL,
                content=query,
                task_type="retrieval_query"
            )
            query_emb = np.array(result['embedding'])
            
            # Cosine similarity
            if len(self.embeddings) == 0:
                return []
                
            scores = np.dot(self.embeddings, query_emb)
            
            # Top K
            top_indices = np.argsort(scores)[-k:][::-1]
            
            results = []
            for idx in top_indices:
                case = self.cases[idx]
                results.append({
                    "id": case["idx"],
                    "score": float(scores[idx]),
                    "problem": case["problem_summary"],
                    "solution": case["solution_summary"],
                    "doc_text": case.get("doc_text", "")
                })
            return results
        except Exception as e:
            print(f"Case Search error: {e}")
            return []

    def answer(self, question):
        # Simple wrapper for the unified agent to call
        results = self.search(question)
        if not results:
            return "No relevant cases found."
            
        # Format for synthesis
        text = "Found similar past cases:\n"
        for r in results:
            text += f"- Case #{r['id']} (Score: {r['score']:.2f}):\n"
            text += f"  Problem: {r['problem']}\n"
            text += f"  Solution: {r['solution']}\n"
            
        return text

if __name__ == "__main__":
    # Test run
    import os
    def _maybe_load_dotenv(dotenv_path):
        path = Path(dotenv_path)
        if not path.exists(): return
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\r"))

    _maybe_load_dotenv(".env")
    genai.configure(api_key=os.environ.get("GOOGLE_API_KEY"))
    
    agent = CaseSearchAgent("test/data/signal_cases_structured.json")
    print(agent.answer("де взяти образ?"))
