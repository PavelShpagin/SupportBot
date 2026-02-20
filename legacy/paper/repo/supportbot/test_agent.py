import os
import sys

# Fix imports
sys.path.append(os.getcwd())

try:
    from paper.repo.supportbot.agent import SupportBot
except ImportError:
    from agent import SupportBot

if __name__ == "__main__":
    bot = SupportBot()
    query = "Where can I find the latest Raspi image?"
    print(f"Q: {query}")
    answer = bot.answer(query)
    print(f"A: {answer}")
