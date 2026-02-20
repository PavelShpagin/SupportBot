from chat_search_agent import ChatSearchAgent

def main():
    print("Initializing Agent...")
    agent = ChatSearchAgent("test/data/chat_index.pkl")
    
    questions = [
        "Яка рама краща для 7 дюймів?",
        "Що робити якщо не армиться?",
        "Хто продає антени?",
        "Як налаштувати ELRS?",
        "Проблема з відео на 1.2"
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
