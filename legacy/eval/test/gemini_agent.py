import os
import sys
import re
import json
import time
import requests
import google.generativeai as genai
from google.generativeai import caching
import datetime
from pathlib import Path

# Fix encoding for Windows console
sys.stdout.reconfigure(encoding='utf-8')

def _maybe_load_dotenv(dotenv_path):
    """Load key=value pairs from .env, stripping CRLF."""
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

# Load .env
_maybe_load_dotenv(".env")

# Configuration
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    raise ValueError("GOOGLE_API_KEY not set")

genai.configure(api_key=GOOGLE_API_KEY)

# Model configuration
MODEL_NAME = "gemini-2.0-flash" 
TARGET_MODEL = "models/gemini-2.0-flash" 

import subprocess

def fetch_doc_content(url):
    """Fetches text content from a URL (Google Doc or generic)."""
    print(f"DEBUG: Fetching content for {url}", flush=True)
    
    # 1. Google Doc Special Handling
    match = re.search(r"/document/d/([a-zA-Z0-9-_]+)", url)
    if match:
        doc_id = match.group(1)
        export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
        try:
            print(f"DEBUG: Requesting Google Doc {export_url}", flush=True)
            response = requests.get(export_url, timeout=10)
            response.raise_for_status()
            print(f"DEBUG: Fetched {len(response.text)} chars", flush=True)
            return response.text
        except Exception as e:
            print(f"Error fetching Google Doc {url}: {e}", flush=True)
            return f"[Error fetching content from {url}]"

    # 2. Generic URL Handling
    try:
        print(f"DEBUG: Requesting Generic URL {url}", flush=True)
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        # Simple HTML cleanup
        text = response.text
        # Remove scripts and styles
        text = re.sub(r'<script\b[^>]*>.*?</script>', '', text, flags=re.DOTALL)
        text = re.sub(r'<style\b[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        # Remove tags
        text = re.sub(r'<[^>]+>', ' ', text)
        # Collapse whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        
        print(f"DEBUG: Fetched {len(text)} chars (cleaned)", flush=True)
        return text
    except Exception as e:
        print(f"Error fetching {url}: {e}", flush=True)
        # Try curl fallback
        try:
            print(f"DEBUG: Trying curl fallback for {url}", flush=True)
            result = subprocess.run(['curl', '-L', '-s', url], capture_output=True, text=True, timeout=10)
            if result.returncode == 0 and result.stdout:
                text = result.stdout
                # Simple HTML cleanup
                text = re.sub(r'<script\b[^>]*>.*?</script>', '', text, flags=re.DOTALL)
                text = re.sub(r'<style\b[^>]*>.*?</style>', '', text, flags=re.DOTALL)
                text = re.sub(r'<[^>]+>', ' ', text)
                text = re.sub(r'\s+', ' ', text).strip()
                print(f"DEBUG: Fetched {len(text)} chars (curl)", flush=True)
                return text
        except Exception as curl_e:
            print(f"Curl failed: {curl_e}", flush=True)
            
        return f"[Error fetching content from {url}]"

def build_context_from_description(description_path):
    """Reads description.txt, fetches docs, and builds context."""
    with open(description_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    context_parts = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Extract URL
        url_match = re.search(r"(https?://\S+)", line)
        if url_match:
            url = url_match.group(1)
            description = line.replace(url, "").strip()
            
            safe_desc = description.encode('ascii', 'replace').decode('ascii')
            print(f"Fetching: {safe_desc} ({url})", flush=True)
            content = fetch_doc_content(url)
            
            part = f"--- SOURCE START ---\n"
            part += f"Description: {description}\n"
            part += f"URL: {url}\n"
            part += f"CONTENT:\n{content}\n"
            part += f"--- SOURCE END ---\n"
            context_parts.append(part)
        else:
            # Just text description
            context_parts.append(f"Info: {line}")

    return "\n".join(context_parts)

class GeminiAgent:
    def __init__(self, context_text, model_name=TARGET_MODEL):
        self.context_text = context_text
        self.model_name = model_name
        self.cache = None
        self.model = None
        self.context_text_fallback = None
        self._setup_cache()

    def _setup_cache(self):
        print("Setting up prompt cache...", flush=True)
        
        system_instruction = """
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
        
        try:
            # Create the cache
            # Note: TTL is required. 
            self.cache = caching.CachedContent.create(
                model=self.model_name,
                display_name="support_bot_context",
                system_instruction=system_instruction,
                contents=[self.context_text],
                ttl=datetime.timedelta(minutes=20),
            )
            
            self.model = genai.GenerativeModel.from_cached_content(cached_content=self.cache)
            print(f"Cache created: {self.cache.name}", flush=True)
        except Exception as e:
            print(f"Cache creation failed (likely due to small context size < 32k tokens): {e}", flush=True)
            print("Falling back to standard context injection.", flush=True)
            self.model = genai.GenerativeModel(self.model_name, system_instruction=system_instruction)
            self.context_text_fallback = self.context_text

    def answer(self, question):
        try:
            if self.cache:
                response = self.model.generate_content(question)
            else:
                # Fallback: Include context in the prompt
                prompt = f"CONTEXT:\n{self.context_text_fallback}\n\nQUESTION:\n{question}"
                response = self.model.generate_content(prompt)
            return response.text
        except Exception as e:
            return f"Error generating answer: {e}"

class Gate:
    def __init__(self, agent):
        self.agent = agent

    def process(self, question):
        print(f"\nProcessing Question: {question}")
        answer = self.agent.answer(question)
        
        if "INSUFFICIENT_INFO" in answer:
            return {
                "action": "tag_admin",
                "response": "@admin (Not enough info in docs to answer this)"
            }
        elif "SKIP" in answer:
            return {
                "action": "skip",
                "response": None
            }
        else:
            return {
                "action": "answer",
                "response": answer
            }

class Judge:
    def __init__(self, model_name="models/gemini-2.0-flash"):
        self.model = genai.GenerativeModel(model_name)

    def evaluate(self, question, answer, context_text):
        prompt = f"""
You are a quality assurance judge for a support bot.

CONTEXT (Knowledge Base):
{context_text}

User Question: {question}
Bot Action/Response: {answer}

EVALUATION RULES:

1. **SKIP**: If the user input is NOT a question or request for help (e.g. "Hi", "Thanks", "Here is a log", "Random text"), the correct action is to SKIP (return None/Empty).
   - If Bot SKIPPED correctly -> Score 10.
   - If Bot Answered/Tagged Admin -> Score 0.

2. **TAG ADMIN**: If the user input IS a question, but the answer is NOT in the CONTEXT:
   - If Bot Tagged Admin -> Score 10.
   - If Bot Answered (Hallucinated) -> Score 0.
   - If Bot Skipped -> Score 0 (It should have flagged it for a human).

3. **ANSWER**: If the user input IS a question and the answer IS in the CONTEXT:
   - If Bot Answered Correctly with Citation -> Score 10.
   - If Bot Answered without Citation -> Score 5.
   - If Bot Tagged Admin/Skipped -> Score 0.

Output a JSON object:
{{
  "score": (0-10),
  "reasoning": "...",
  "correct_action": (true/false)
}}
"""
        try:
            response = self.model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
            return json.loads(response.text)
        except Exception as e:
            return {"error": str(e)}

def main():
    description_path = "test/description.txt"
    if not os.path.exists(description_path):
        print(f"File not found: {description_path}")
        return

    # 1. Build Context
    print("Building context...")
    context = build_context_from_description(description_path)
    
    # 2. Initialize Agent
    agent = GeminiAgent(context)
    
    # 3. Initialize Gate
    gate = Gate(agent)
    
    # 4. Initialize Judge
    judge = Judge()
    
    # 5. Test with some questions (simulated)
    # We can load from test/data/signal_messages.json if needed, but for now let's use some hardcoded ones relevant to the doc I saw.
    test_questions = [
        "Привіт", # Greeting -> SKIP
        "Дякую за допомогу", # Gratitude -> SKIP
        "Ось посилання на лог: https://example.com", # Statement/Link -> SKIP
        "Хвіст Вирія", # Random statement -> SKIP
        "У мене не працює відео", # Vague problem -> TAG ADMIN (since it's a problem statement, arguably a request for help, but info missing)
        "Як увімкнути?", # Ambiguous -> TAG ADMIN
        "Де скачати прошивку?", # Clear question -> ANSWER
    ]

    print("\n--- Starting Test ---")
    
    for q in test_questions:
        result = gate.process(q)
        print(f"Action: {result['action']}")
        print(f"Bot Response: {result['response']}")
        
        # Judge
        eval_result = judge.evaluate(q, result['response'], context)
        print(f"Judge: Score={eval_result.get('score')}, Reasoning={eval_result.get('reasoning')}")
        print("-" * 30)

if __name__ == "__main__":
    main()
