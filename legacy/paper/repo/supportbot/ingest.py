import os
import json
import requests
import hashlib
import time
from typing import List, Dict, Any
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import google.generativeai as genai
try:
    from .storage import SimpleVectorStore, LocalBlobStorage
except ImportError:
    from storage import SimpleVectorStore, LocalBlobStorage

# Configuration
MODEL_NAME = "gemini-2.0-flash"
EMBEDDING_MODEL = "models/gemini-embedding-001"
MAX_PAGES = 50
MAX_DEPTH = 2

class Ingester:
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

    def get_embedding(self, text: str) -> List[float]:
        try:
            result = genai.embed_content(
                model=EMBEDDING_MODEL,
                content=text,
                task_type="retrieval_document"
            )
            return result['embedding']
        except Exception as e:
            print(f"Embedding error: {e}")
            return []

    def crawl_docs(self, start_urls: List[str]):
        visited = set()
        queue = [(url, 0) for url in start_urls]
        count = 0
        
        while queue and count < MAX_PAGES:
            url, depth = queue.pop(0)
            if url in visited or depth > MAX_DEPTH:
                continue
            
            visited.add(url)
            print(f"Crawling: {url} (Depth: {depth})")
            
            try:
                response = requests.get(url, timeout=10)
                if response.status_code != 200:
                    print(f"Failed to fetch {url}: {response.status_code}")
                    continue
                
                soup = BeautifulSoup(response.content, 'html.parser')
                title = str(soup.title.string) if soup.title else url
                text = soup.get_text(separator=' ', strip=True)
                
                # Store content
                blob_id = hashlib.sha256(url.encode()).hexdigest() + ".html"
                self.blob_storage.save(blob_id, response.text)
                
                # Generate embedding
                embedding = self.get_embedding(text[:2000]) # Limit for embedding
                
                # Store metadata
                doc = {
                    'url': url,
                    'title': title,
                    'text': text[:5000], # Store snippet
                    'blob_id': blob_id,
                    'type': 'doc'
                }
                self.docs_store.add([doc], [embedding])
                count += 1
                
                # Extract links
                if depth < MAX_DEPTH:
                    for link in soup.find_all('a', href=True):
                        href = link['href']
                        full_url = urljoin(url, href)
                        parsed = urlparse(full_url)
                        if parsed.netloc == urlparse(url).netloc: # Only internal links
                            if full_url not in visited:
                                queue.append((full_url, depth + 1))
                                
                time.sleep(1) # Be polite
                
            except Exception as e:
                print(f"Error crawling {url}: {e}")

    def ingest_cases(self, cases_path: str):
        if not os.path.exists(cases_path):
            print(f"Cases file not found: {cases_path}")
            return
            
        with open(cases_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
        cases = data.get('cases', [])
        print(f"Ingesting {len(cases)} cases...")
        
        batch_docs = []
        batch_embeddings = []
        
        for case in cases:
            case_id = str(case.get('idx', 'unknown'))
            summary = case.get('problem_summary', '')
            solution = case.get('solution_summary', '')
            full_text = f"Problem: {summary}\nSolution: {solution}\nTags: {', '.join(case.get('tags', []))}"
            
            # Store HTML content if available (mocking for now as JSON doesn't have full HTML)
            blob_id = f"case_{case_id}.html"
            html_content = f"<html><body><h1>Case #{case_id}</h1><p><b>Problem:</b> {summary}</p><p><b>Solution:</b> {solution}</p></body></html>"
            self.blob_storage.save(blob_id, html_content)
            
            # Generate embedding
            embedding = case.get('embedding')
            if not embedding:
                embedding = self.get_embedding(full_text)
            
            if embedding:
                doc = {
                    'id': case_id,
                    'problem_summary': summary,
                    'solution_summary': solution,
                    'blob_id': blob_id,
                    'type': 'case'
                }
                batch_docs.append(doc)
                batch_embeddings.append(embedding)
                
            if len(batch_docs) >= 10:
                self.cases_store.add(batch_docs, batch_embeddings)
                batch_docs = []
                batch_embeddings = []
                print(".", end="", flush=True)
                
        if batch_docs:
            self.cases_store.add(batch_docs, batch_embeddings)
        print("\nCases ingestion complete.")

if __name__ == "__main__":
    ingester = Ingester()
    
    # Ingest Docs
    # Read URLs from paper/repo/docs.txt
    docs_path = "paper/repo/docs.txt"
    if os.path.exists(docs_path):
        with open(docs_path, 'r') as f:
            lines = f.readlines()
        urls = [line.strip() for line in lines if line.strip().startswith("http")]
        print(f"Found {len(urls)} doc URLs to crawl.")
        ingester.crawl_docs(urls)
    
    # Ingest Cases
    cases_path = "test/data/signal_cases_structured.json"
    ingester.ingest_cases(cases_path)
