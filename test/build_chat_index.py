import json
import os
import sys
import time
import pickle
import google.generativeai as genai
from pathlib import Path
from tqdm import tqdm

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

EMBEDDING_MODEL = "models/gemini-embedding-001"

def load_and_prep_messages(path):
    print(f"Loading messages from {path}...")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    messages = data.get("messages", [])
    # Filter for useful text messages
    # We want incoming messages with body text.
    # We might also want to include attachments descriptions if we had them, but for now just body.
    
    clean_msgs = []
    for i, m in enumerate(messages):
        body = m.get("body", "").strip()
        if m.get("type") == "incoming" and body:
            # Basic cleanup: remove attachment tags for embedding (optional, but good for noise reduction)
            # Actually, keeping them might help if someone searches for "image of..."
            # Let's keep it simple.
            clean_msgs.append({
                "id": m.get("id"),
                "original_index": i, # To find context later
                "timestamp": m.get("timestamp"),
                "sender": m.get("sender"),
                "text": body
            })
    
    print(f"Found {len(clean_msgs)} indexable messages.")
    return clean_msgs

def batch_embed(texts, batch_size=100):
    embeddings = []
    for i in tqdm(range(0, len(texts), batch_size), desc="Embedding"):
        batch = texts[i:i+batch_size]
        try:
            result = genai.embed_content(
                model=EMBEDDING_MODEL,
                content=batch,
                task_type="retrieval_document",
                title="Support Chat History"
            )
            embeddings.extend(result['embedding'])
            time.sleep(1) # Rate limit safety
        except Exception as e:
            print(f"Error embedding batch {i}: {e}")
            # Fill with zeros or skip? Better to skip or retry.
            # For prototype, let's just append None and filter later
            embeddings.extend([None] * len(batch))
            
    return embeddings

def main():
    messages_path = "test/data/signal_messages.json"
    index_path = "test/data/chat_index.pkl"
    
    if not os.path.exists(messages_path):
        print(f"File not found: {messages_path}")
        return

    messages = load_and_prep_messages(messages_path)
    
    # Extract texts
    texts = [m["text"] for m in messages]
    
    print("Generating embeddings...")
    embeddings = batch_embed(texts)
    
    # Filter out failed embeddings
    valid_data = []
    for m, emb in zip(messages, embeddings):
        if emb is not None:
            m["embedding"] = emb
            valid_data.append(m)
            
    print(f"Successfully indexed {len(valid_data)} messages.")
    
    print(f"Saving index to {index_path}...")
    with open(index_path, "wb") as f:
        pickle.dump(valid_data, f)
    print("Done.")

if __name__ == "__main__":
    main()
