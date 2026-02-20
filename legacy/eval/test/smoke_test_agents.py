#!/usr/bin/env python3
"""
Smoke Test for All Agents
Shows how each agent works and outputs HTML citations
"""

import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

# Setup paths
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "test" else SCRIPT_DIR
os.chdir(PROJECT_ROOT)

# Load environment
def _load_env():
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))

_load_env()

# Test questions
TEST_QUESTIONS = [
    "Як налаштувати FS_EKF_THRESH?",  # Technical config question
    "Який польотник краще використовувати?",  # Hardware recommendation
    "Привіт",  # Should be SKIP (noise)
]

def print_separator(title):
    print("\n" + "="*70)
    print(f"  {title}")
    print("="*70)

def test_docsonly():
    """Test DocsOnly agent (gemini_agent.py)"""
    print_separator("1. DocsOnly (gemini_agent.py)")
    print("Uses: Official documentation only")
    print("Output: Answer with [Source: URL] citations\n")
    
    from gemini_agent import GeminiAgent, build_context_from_description
    
    description_path = PROJECT_ROOT / "test" / "description.txt"
    context = build_context_from_description(str(description_path))
    agent = GeminiAgent(context)
    
    for q in TEST_QUESTIONS[:2]:
        print(f"\n📝 Q: {q}")
        try:
            answer = agent.answer(q)
            print(f"💬 A: {answer[:500]}...")
        except Exception as e:
            print(f"❌ Error: {e}")

def test_chatrag():
    """Test ChatRAG agent (chat_search_agent.py)"""
    print_separator("2. ChatRAG (chat_search_agent.py)")
    print("Uses: Chat history embeddings search")
    print("Output: Answer with [Date, UUID] or HTML <a> citations\n")
    
    from chat_search_agent import ChatSearchAgent
    
    index_path = PROJECT_ROOT / "test" / "data" / "chat_index.pkl"
    agent = ChatSearchAgent(str(index_path), public_url="http://localhost:3000")
    
    for q in TEST_QUESTIONS[:2]:
        print(f"\n📝 Q: {q}")
        try:
            # Without HTML
            result = agent.answer(q, return_details=True, html_citations=False)
            print(f"💬 A (plain): {result['answer'][:300]}...")
            
            # With HTML
            result_html = agent.answer(q, return_details=True, html_citations=True)
            print(f"🌐 A (HTML): {result_html['answer'][:300]}...")
        except Exception as e:
            print(f"❌ Error: {e}")

def test_caserag():
    """Test Case RAG agent (case_search_agent.py)"""
    print_separator("3. CaseRAG (case_search_agent.py)")
    print("Uses: Structured solved tickets (cases)")
    print("Output: Answer with Case #N or HTML <a> citations\n")
    
    from case_search_agent import CaseSearchAgent
    
    cases_path = PROJECT_ROOT / "test" / "data" / "signal_cases_structured.json"
    agent = CaseSearchAgent(str(cases_path), public_url="http://localhost:3000")
    
    for q in TEST_QUESTIONS[:2]:
        print(f"\n📝 Q: {q}")
        try:
            # Without HTML
            answer = agent.answer(q, html_citations=False)
            print(f"💬 A (plain): {answer[:300]}...")
            
            # With HTML
            answer_html = agent.answer(q, html_citations=True)
            print(f"🌐 A (HTML): {answer_html[:300]}...")
        except Exception as e:
            print(f"❌ Error: {e}")

def test_docschat():
    """Test DocsChat agent (unified_agent.py)"""
    print_separator("4. DocsChat (unified_agent.py)")
    print("Uses: Docs + Chat (synthesis)")
    print("Output: Synthesized answer with mixed citations\n")
    
    from unified_agent import UnifiedAgent
    
    agent = UnifiedAgent()
    
    for q in TEST_QUESTIONS[:2]:
        print(f"\n📝 Q: {q}")
        try:
            answer = agent.answer(q)
            print(f"💬 A: {answer[:500]}...")
        except Exception as e:
            print(f"❌ Error: {e}")

def test_supportbot():
    """Test SupportBot/Ultimate agent (ultimate_agent.py)"""
    print_separator("5. SupportBot (ultimate_agent.py)")
    print("Uses: Chat (primary) + Cases + Docs (enrichment)")
    print("Output: Chat-first answer with HTML citations\n")
    
    from ultimate_agent import UltimateAgent
    
    agent = UltimateAgent(public_url="http://localhost:3000")
    
    for q in TEST_QUESTIONS:
        print(f"\n📝 Q: {q}")
        try:
            answer = agent.answer(q)
            if answer == "SKIP":
                print(f"⏭️ A: SKIP (noise detected)")
            elif answer == "INSUFFICIENT_INFO":
                print(f"❓ A: INSUFFICIENT_INFO (need admin)")
            else:
                print(f"💬 A: {answer[:500]}...")
        except Exception as e:
            print(f"❌ Error: {e}")

def test_cleanagent():
    """Test CleanAgent (clean_agent.py)"""
    print_separator("6. CleanAgent (clean_agent.py)")
    print("Uses: Gate → [Docs | Cases | Chat] → Aggregator")
    print("Output: SKIP, TAG_ADMIN, or Answer with citations\n")
    
    from clean_agent import CleanAgent
    
    agent = CleanAgent()
    
    for q in TEST_QUESTIONS:
        print(f"\n📝 Q: {q}")
        try:
            answer = agent.answer(q)
            if answer == "SKIP":
                print(f"⏭️ A: SKIP (noise detected)")
            elif "TAG_ADMIN" in answer or "INSUFFICIENT" in answer:
                print(f"❓ A: {answer}")
            else:
                print(f"💬 A: {answer[:500]}...")
        except Exception as e:
            print(f"❌ Error: {e}")

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Smoke test agents")
    parser.add_argument("--agent", type=str, default="all",
                        help="Agent to test: docsonly, chatrag, caserag, docschat, supportbot, cleanagent, all")
    parser.add_argument("-q", "--question", type=str, help="Custom question to test")
    args = parser.parse_args()
    
    if args.question:
        global TEST_QUESTIONS
        TEST_QUESTIONS = [args.question]
    
    print("\n🧪 AGENT SMOKE TEST")
    print("Testing each agent to see how they work and produce citations\n")
    
    agents = {
        "docsonly": test_docsonly,
        "chatrag": test_chatrag,
        "caserag": test_caserag,
        "docschat": test_docschat,
        "supportbot": test_supportbot,
        "cleanagent": test_cleanagent,
    }
    
    if args.agent == "all":
        for name, func in agents.items():
            try:
                func()
            except Exception as e:
                print(f"❌ Failed to test {name}: {e}")
    elif args.agent in agents:
        agents[args.agent]()
    else:
        print(f"Unknown agent: {args.agent}")
        print(f"Available: {', '.join(agents.keys())}, all")

if __name__ == "__main__":
    main()
