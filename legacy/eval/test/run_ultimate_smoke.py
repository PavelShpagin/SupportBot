from ultimate_agent import UltimateAgent

def main():
    print("Initializing Ultimate Agent...")
    agent = UltimateAgent()
    
    questions = [
        "Привіт", # Should SKIP
        "Дякую", # Should SKIP
        "Хвіст Вирія", # Should SKIP
        "Яка дальність польоту вдень?", # Docs Answer
        "Де взяти прошивку?", # Docs Answer
        "Що робити якщо не армиться?", # Chat/Case Answer
        "Проблема з відео на 1.2", # Chat Answer
        "Як налаштувати ELRS?", # Chat Answer
        "У мене не працює відео", # Vague -> INSUFFICIENT_INFO (Tag Admin)
    ]
    
    print("\n--- Smoke Test ---")
    for q in questions:
        print(f"\nQ: {q}")
        try:
            ans = agent.answer(q)
            print(f"A: {ans}")
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    main()
