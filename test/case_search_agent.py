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
    def __init__(self, cases_path, public_url=None):
        self.cases_path = cases_path
        self.public_url = public_url or "http://localhost:3000"  # Default for citations
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
            
    def search(self, query, group_id=None, db=None, k=3):
        """
        Searches cases for relevant problems/solutions using semantic similarity.
        
        Args:
            query: User question to search for
            group_id: Optional group filter (not yet implemented)
            db: Optional database connection (not yet implemented)
            k: Number of results to return
            
        Returns:
            List of case dicts with id, score, problem, solution, doc_text, evidence_ids
        """
        if not self.cases or len(self.embeddings) == 0:
            return []
            
        try:
            # Embed query using Gemini embeddings
            result = genai.embed_content(
                model=EMBEDDING_MODEL,
                content=query,
                task_type="retrieval_query"
            )
            query_emb = np.array(result['embedding'])
            
            # Cosine similarity (embeddings are already normalized)
            scores = np.dot(self.embeddings, query_emb)
            
            # Top K indices
            top_indices = np.argsort(scores)[-k:][::-1]
            
            results = []
            for idx in top_indices:
                case = self.cases[idx]
                results.append({
                    "id": case["idx"],
                    "score": float(scores[idx]),
                    "problem": case.get("problem_summary", case.get("problem_title", "")),
                    "solution": case.get("solution_summary", ""),
                    "doc_text": case.get("doc_text", ""),
                    "evidence_ids": case.get("evidence_ids", []),
                    "tags": case.get("tags", [])
                })
            return results
        except Exception as e:
            print(f"Case Search error: {e}")
            return []

    def answer(self, question, group_id=None, db=None, html_citations=True):
        """
        Search for relevant cases and return formatted answer.
        
        Args:
            question: User question
            group_id: Optional group filter
            db: Optional database connection
            html_citations: If True, return HTML-formatted citations
        """
        results = self.search(question, group_id=group_id, db=db, k=3)
        if not results or all(r['score'] < 0.5 for r in results):
            return "No relevant cases found."
        
        # Filter to relevant results only
        relevant = [r for r in results if r['score'] >= 0.5]
        if not relevant:
            return "No relevant cases found."
        
        if html_citations:
            # HTML format with clickable links
            text = "<b>Found similar past cases:</b>\n\n"
            for r in relevant:
                case_url = f"{self.public_url}/case/{r['id']}"
                text += f"<b>Case #{r['id']}</b> (relevance: {r['score']:.0%})\n"
                text += f"<b>Problem:</b> {r['problem']}\n"
                text += f"<b>Solution:</b> {r['solution']}\n"
                text += f'<a href="{case_url}">[View Case]</a>\n\n'
        else:
            # Plain text format
            text = "Found similar past cases:\n\n"
            for r in relevant:
                text += f"Case #{r['id']} (relevance: {r['score']:.0%}):\n"
                text += f"  Problem: {r['problem']}\n"
                text += f"  Solution: {r['solution']}\n"
                text += f"  [Source: {self.public_url}/case/{r['id']}]\n\n"
            
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
