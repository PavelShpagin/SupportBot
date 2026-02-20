import json
import os
import sys
from ultimate_agent import UltimateAgent

# Fix encoding
sys.stdout.reconfigure(encoding='utf-8')

def load_first_messages(path, limit=10):
    print(f"Loading messages from {path}...")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    messages = data.get("messages", [])
    incoming = [
        m for m in messages 
        if m.get("type") == "incoming" and m.get("body", "").strip()
    ]
    
    # Take the FIRST N messages (oldest)
    selected = incoming[:limit]
    print(f"Selected FIRST {len(selected)} messages.")
    return selected

def main():
    agent = UltimateAgent()
    messages = load_first_messages("test/data/signal_messages.json", limit=10)
    
    print("\n--- Examples from First 1000 Messages ---")
    for i, msg in enumerate(messages):
        q = msg.get("body", "").strip()
        print(f"\n[{i+1}] User: {q}")
        ans = agent.answer(q)
        print(f"Bot: {ans}")

if __name__ == "__main__":
    main()
