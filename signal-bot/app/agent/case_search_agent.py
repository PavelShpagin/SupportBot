import json
import numpy as np
import sys
import re

# Fix encoding
sys.stdout.reconfigure(encoding='utf-8')

class CaseSearchAgent:
    def __init__(self, rag=None, llm=None, public_url="https://supportbot.info"):
        self.rag = rag
        self.llm = llm
        self.public_url = public_url
        
    def search(self, query, k=3):
        """Searches cases for relevant problems/solutions."""
        print(f"DEBUG: Searching Cases for '{query}'...")
        if not self.rag or not self.llm:
            print("CaseSearchAgent: RAG or LLM not initialized.")
            return []
            
        try:
            # Embed query
            query_emb = self.llm.embed(text=query)
            
            # Search global cases
            results = self.rag.search_all_cases(embedding=query_emb, k=k)
            
            formatted_results = []
            for r in results:
                # Parse doc text to extract problem/solution if possible
                # Doc format: Title\nProblem\nSolution\nTags
                doc = r.get("document", "")
                lines = doc.split("\n")
                problem = ""
                solution = ""
                
                # Basic parsing based on expected format
                for line in lines:
                    if line.startswith("Проблема:"):
                        problem = line.replace("Проблема:", "").strip()
                    elif line.startswith("Рішення:"):
                        solution = line.replace("Рішення:", "").strip()
                
                # Fallback if parsing failed
                if not problem:
                    problem = lines[1] if len(lines) > 1 else "Unknown"
                if not solution:
                    solution = lines[2] if len(lines) > 2 else "See details"

                formatted_results.append({
                    "id": r["case_id"],
                    "score": 1.0 - (r.get("distance") or 0.5), # Chroma returns distance, convert to similarity
                    "problem": problem,
                    "solution": solution,
                    "doc_text": doc
                })
            return formatted_results
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
            text += f"  Link: {self.public_url}/case/{r['id']}\n"
            
        return text
