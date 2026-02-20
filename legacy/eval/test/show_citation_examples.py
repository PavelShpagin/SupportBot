import sys
import os
import json
from ultimate_agent import UltimateAgent

# Fix encoding
sys.stdout.reconfigure(encoding='utf-8')

def main():
    if not os.environ.get("GOOGLE_API_KEY"):
        print("GOOGLE_API_KEY not found in env.")
        return

    agent = UltimateAgent()
    
    questions = [
        "Як налаштувати PID для 7-дюймового дрона?",
        "Де взяти прошивку для Matek H743?",
        "Що робити, якщо відео з камери 'пливе' (желе)?",
        "Як підключити ELRS приймач?",
        "Хто такий Святослав з Ультраконтакт?"
    ]
    
    print("\n--- CITATION EXAMPLES ---\n")
    
    for q in questions:
        print(f"Q: {q}")
        ans = agent.answer(q)
        print(f"A: {ans}\n")
        print("-" * 40 + "\n")

if __name__ == "__main__":
    main()
