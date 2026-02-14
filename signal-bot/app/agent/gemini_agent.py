import os
import sys
import re
import requests
import google.generativeai as genai
from google.generativeai import caching
import datetime
from pathlib import Path
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

# Fix encoding for Windows console
sys.stdout.reconfigure(encoding='utf-8')

# Configuration
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)

# Model configuration
MODEL_NAME = "gemini-2.0-flash" 
TARGET_MODEL = "models/gemini-2.0-flash" 

def extract_doc_id(url):
    """Extract Google Doc ID from URL."""
    match = re.search(r"/document/d/([a-zA-Z0-9-_]+)", url)
    return match.group(1) if match else None

def fetch_doc_recursive(start_urls, max_depth=1, max_docs=10):
    """
    Recursively fetches Google Docs content and images.
    Returns a list of content parts compatible with Gemini API.
    """
    queue = [(url, 0) for url in start_urls]
    visited = set()
    content_parts = []
    
    docs_processed = 0
    
    while queue and docs_processed < max_docs:
        url, depth = queue.pop(0)
        doc_id = extract_doc_id(url)
        
        if not doc_id or doc_id in visited:
            continue
            
        visited.add(doc_id)
        docs_processed += 1
        
        print(f"Fetching (depth={depth}): {url}", flush=True)
        
        # Export as HTML to get structure and images
        export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=html"
        
        try:
            response = requests.get(export_url, timeout=15)
            response.raise_for_status()
            html_content = response.text
            
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Start a new section in context
            content_parts.append(f"\n\n--- DOCUMENT START: {url} ---\n")
            
            # Extract text and images in order
            # We walk the body
            body = soup.body
            if not body:
                continue
                
            current_text = ""
            
            for element in body.descendants:
                if element.name == 'img':
                    # Flush text buffer
                    if current_text.strip():
                        content_parts.append(current_text)
                        current_text = ""
                    
                    # Handle image
                    img_src = element.get('src')
                    if img_src:
                        try:
                            # Google Docs export images are usually accessible directly
                            img_resp = requests.get(img_src, timeout=10)
                            if img_resp.status_code == 200:
                                mime_type = img_resp.headers.get('Content-Type', 'image/jpeg')
                                content_parts.append({
                                    "mime_type": mime_type,
                                    "data": img_resp.content
                                })
                                print(f"  - Captured image ({len(img_resp.content)} bytes)", flush=True)
                        except Exception as e:
                            print(f"  - Failed to fetch image: {e}", flush=True)
                            
                elif isinstance(element, str):
                    text = element.strip()
                    if text:
                        current_text += text + " "
                
                elif element.name in ['p', 'h1', 'h2', 'h3', 'li', 'br']:
                    current_text += "\n"
            
            # Flush remaining text
            if current_text.strip():
                content_parts.append(current_text)
                
            content_parts.append(f"\n--- DOCUMENT END: {url} ---\n")
            
            # Find links for recursion
            if depth < max_depth:
                for link in soup.find_all('a', href=True):
                    href = link['href']
                    # Google redirects links via google.com/url?q=...
                    if "google.com/url" in href:
                        parsed = urlparse(href)
                        query = dict(q.split('=') for q in parsed.query.split('&') if '=' in q)
                        real_url = query.get('q')
                        if real_url and "docs.google.com/document/d/" in real_url:
                            if extract_doc_id(real_url) not in visited:
                                queue.append((real_url, depth + 1))
                    elif "docs.google.com/document/d/" in href:
                         if extract_doc_id(href) not in visited:
                                queue.append((href, depth + 1))

        except Exception as e:
            print(f"Error fetching {url}: {e}", flush=True)
            content_parts.append(f"[Error fetching {url}: {e}]")
            
    return content_parts

def build_context_from_urls(urls):
    """Fetches docs recursively and builds multimodal context."""
    return fetch_doc_recursive(urls, max_depth=1)

def build_context_from_description(description_path):
    """Reads description.txt and fetches docs recursively."""
    with open(description_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    urls = []
    text_info = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        url_match = re.search(r"(https?://\S+)", line)
        if url_match:
            urls.append(url_match.group(1))
        else:
            text_info.append(line)
            
    # Fetch content
    parts = fetch_doc_recursive(urls, max_depth=1)
    
    # Prepend text info
    if text_info:
        header = "Global Info:\n" + "\n".join(text_info) + "\n\n"
        parts.insert(0, header)
        
    return parts

class GeminiAgent:
    def __init__(self, context_parts, model_name=TARGET_MODEL):
        self.context_parts = context_parts
        self.model_name = model_name
        self.cache = None
        self.model = None
        
        self.system_instruction = """
You are a technical support automation system. Your goal is to strictly filter and answer questions based on the provided documentation.

INPUT CLASSIFICATION & BEHAVIOR:

1. **ANALYZE**: Is the user input a QUESTION or a REQUEST FOR HELP?
   - **NO** (Greetings like "Hi", Gratitude like "Thanks", Statements like "Here is a log", Random phrases like "Tail"): 
     -> Output exactly: "SKIP"
   - **YES**: Proceed to step 2.

2. **EVALUATE**: Do you have the information in the provided CONTEXT to answer it?
   - **NO** (The topic is not covered, or you cannot perform the requested action like analyzing a log file):
     -> Output exactly: "INSUFFICIENT_INFO"
   - **YES**:
     -> Provide a clear, technical answer.
     -> You MUST cite the specific Source URL and section.
     -> Format: "Answer... [Source: URL, Section: ...]"

CONTEXT (Google Docs):
"""
        self._setup_cache()

    def _setup_cache(self):
        print("Setting up prompt cache...", flush=True)
        
        try:
            # Create the cache
            # contents expects a list of Content objects or compatible structure
            # We wrap our parts in a single 'user' message for the cache context
            
            self.cache = caching.CachedContent.create(
                model=self.model_name,
                display_name="support_bot_context",
                system_instruction=self.system_instruction,
                contents=[{'role': 'user', 'parts': self.context_parts}],
                ttl=datetime.timedelta(minutes=20),
            )
            
            self.model = genai.GenerativeModel.from_cached_content(cached_content=self.cache)
            print(f"Cache created: {self.cache.name}", flush=True)
        except Exception as e:
            print(f"Cache creation failed: {e}", flush=True)
            print("Falling back to standard context injection.", flush=True)
            self.model = genai.GenerativeModel(self.model_name)

    def answer(self, question):
        try:
            if self.cache:
                response = self.model.generate_content(question)
            else:
                # Fallback: Construct prompt manually
                # Note: If context_parts contains images, we pass them as a list
                prompt_parts = [self.system_instruction] + self.context_parts + ["\n\nQUESTION:\n" + question]
                response = self.model.generate_content(prompt_parts)
            return response.text
        except Exception as e:
            return f"INSUFFICIENT_INFO (Error: {e})"
