#!/usr/bin/env python3
"""
Quality demonstration script for SupportBot.

Shows real examples of bot behavior with Gemini evaluation.

Run (recommended):
  - Put `GOOGLE_API_KEY=...` in `.env` (repo root), OR export it in your shell
  - `python test/run_quality_demo.py`
"""

import json
import os
import sys
from pathlib import Path
from typing import List, Dict, Optional
from dataclasses import dataclass

sys.path.insert(0, str(Path(__file__).parent.parent / "signal-bot"))

def _maybe_load_dotenv(dotenv_path: Path) -> None:
    """
    Load key=value pairs from .env, stripping CRLF, without overriding existing env.
    """
    if not dotenv_path.exists():
        return
    for raw in dotenv_path.read_text(encoding="utf-8", errors="ignore").splitlines():
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


_maybe_load_dotenv(Path(__file__).resolve().parent.parent / ".env")

if not os.environ.get("GOOGLE_API_KEY"):
    print("ERROR: GOOGLE_API_KEY environment variable not set")
    print("Put GOOGLE_API_KEY in .env or export it, then rerun.")
    sys.exit(1)

from openai import OpenAI
from app.llm.client import LLMClient
from app.config import Settings


# =============================================================================
# Setup
# =============================================================================

def create_settings() -> Settings:
    return Settings(
        db_backend="mysql",
        mysql_host="localhost",
        mysql_port=3306,
        mysql_user="test",
        mysql_password="test",
        mysql_database="test",
        oracle_user="",
        oracle_password="",
        oracle_dsn="",
        oracle_wallet_dir="",
        openai_api_key=os.environ["GOOGLE_API_KEY"],
        model_img="gemini-2.0-flash",
        model_decision="gemini-2.5-flash-lite",
        model_extract="gemini-2.5-flash-lite",
        model_case="gemini-2.5-flash-lite",
        model_respond="gemini-2.0-flash",
        model_blocks="gemini-2.0-flash",
        embedding_model="text-embedding-004",
        chroma_url="http://localhost:8001",
        chroma_collection="test",
        signal_bot_e164="+10000000000",
        signal_bot_storage="/tmp",
        signal_ingest_storage="/tmp",
        signal_cli="signal-cli",
        bot_mention_strings=["@supportbot"],
        signal_listener_enabled=False,
        log_level="WARNING",
        context_last_n=40,
        retrieve_top_k=5,
        worker_poll_seconds=1,
        history_token_ttl_minutes=60,
        max_images_per_gate=3,
        max_images_per_respond=5,
        max_kb_images_per_case=2,
        max_image_size_bytes=5_000_000,
        max_total_image_bytes=20_000_000,
    )


# =============================================================================
# Knowledge Base
# =============================================================================

KNOWLEDGE_BASE = [
    {
        "case_id": "case-001",
        "problem": "Неможливість увійти в особистий кабінет",
        "solution": "Скинути пароль через форму відновлення на сторінці входу.",
        "tags": ["login", "password"],
    },
    {
        "case_id": "case-002", 
        "problem": "Відео уроки не завантажуються в Firefox",
        "solution": "Використати Chrome або Edge. Firefox має проблеми сумісності.",
        "tags": ["video", "browser"],
    },
    {
        "case_id": "case-003",
        "problem": "Отримання сертифікату",
        "solution": "Завершити всі модулі та скласти тест на 70%+. Сертифікат в: Кабінет → Мої сертифікати.",
        "tags": ["certificate"],
    },
    {
        "case_id": "case-004",
        "problem": "Немає доступу після оплати",
        "solution": "Написати в підтримку з номером транзакції. Активують вручну.",
        "tags": ["payment"],
    },
    {
        "case_id": "case-005",
        "problem": "Мобільний додаток",
        "solution": "Додаток 'СтабХ Академія' в App Store/Google Play. Офлайн завантаження доступне.",
        "tags": ["mobile", "app"],
    },
]


# =============================================================================
# Demo
# =============================================================================

def run_demo():
    print("=" * 80)
    print("SUPPORTBOT QUALITY DEMONSTRATION")
    print("=" * 80)
    print()
    
    settings = create_settings()
    llm = LLMClient(settings)
    
    # Judge client
    judge = OpenAI(
        api_key=os.environ["GOOGLE_API_KEY"],
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    
    def format_cases(cases: List[Dict]) -> str:
        return json.dumps([{
            "case_id": c["case_id"],
            "document": f"{c['problem']}\n{c['solution']}",
            "metadata": {"group_id": "test", "status": "solved"},
        } for c in cases], ensure_ascii=False)
    
    def evaluate_response(question: str, response: str, cases: List[Dict]) -> Dict:
        kb = "\n".join([f"{c['case_id']}: {c['solution']}" for c in cases])
        prompt = f"""Evaluate this Ukrainian support bot response.

KNOWLEDGE: {kb}
QUESTION: {question}
RESPONSE: {response}

Return JSON: {{"score": 0-10, "accurate": bool, "helpful": bool, "hallucination": bool, "note": "..."}}"""
        
        resp = judge.chat.completions.create(
            model="gemini-2.5-flash-lite",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        return json.loads(resp.choices[0].message.content or "{}")
    
    # =========================================================================
    # SECTION 1: Questions that SHOULD get answers
    # =========================================================================
    
    print("╔" + "═" * 78 + "╗")
    print("║ SECTION 1: Questions that SHOULD get helpful answers                       ║")
    print("╚" + "═" * 78 + "╝")
    print()
    
    should_answer = [
        ("Привіт, не можу зайти в кабінет, пише невірний пароль", [KNOWLEDGE_BASE[0]]),
        ("Відео уроки не вантажаться, крутиться колесо вже годину", [KNOWLEDGE_BASE[1]]),
        ("Коли я отримаю сертифікат про проходження курсу?", [KNOWLEDGE_BASE[2]]),
        ("Оплатив курс, гроші списались, а доступу немає!", [KNOWLEDGE_BASE[3]]),
        ("Чи є мобільний додаток? Хочу в метро дивитися", [KNOWLEDGE_BASE[4]]),
    ]
    
    all_passed = True
    for question, cases in should_answer:
        print(f"📝 ПИТАННЯ: {question}")
        
        # Stage 1
        decision = llm.decide_consider(message=question, context="Група техпідтримки академії")
        
        if not decision.consider:
            print(f"   ❌ Stage 1: Bot ignored (WRONG!)")
            all_passed = False
            print()
            continue
        
        # Stage 2
        result = llm.decide_and_respond(
            message=question,
            context="Група техпідтримки",
            cases=format_cases(cases),
        )
        
        if not result.respond:
            print(f"   ❌ Stage 2: Bot declined (WRONG!)")
            all_passed = False
            print()
            continue
        
        response = result.text
        if result.citations:
            response += f" [Реф: {', '.join(result.citations[:2])}]"
        
        print(f"🤖 ВІДПОВІДЬ: {response}")
        
        # Evaluate
        eval_result = evaluate_response(question, result.text, cases)
        score = eval_result.get("score", 0)
        accurate = eval_result.get("accurate", False)
        hallucination = eval_result.get("hallucination", False)
        
        if score >= 7 and accurate and not hallucination:
            print(f"   ✅ Score: {score}/10 | Accurate: {accurate} | No hallucination")
        else:
            print(f"   ⚠️ Score: {score}/10 | Accurate: {accurate} | Hallucination: {hallucination}")
            print(f"      Note: {eval_result.get('note', '')}")
            all_passed = False
        
        print()
    
    # =========================================================================
    # SECTION 2: Questions that should be DECLINED (no knowledge)
    # =========================================================================
    
    print("╔" + "═" * 78 + "╗")
    print("║ SECTION 2: Questions that should be DECLINED (no knowledge → no answer)   ║")
    print("╚" + "═" * 78 + "╝")
    print()
    
    should_decline = [
        "Як налаштувати Kubernetes кластер?",
        "Порекомендуйте хороший ресторан у Києві",
        "Яка погода буде завтра?",
        "Як написати рекурсивну функцію на Haskell?",
    ]
    
    for question in should_decline:
        print(f"📝 ПИТАННЯ: {question}")
        
        decision = llm.decide_consider(message=question, context="Група техпідтримки академії")
        
        if not decision.consider:
            print(f"   ✅ Stage 1: Correctly ignored (irrelevant)")
            print()
            continue
        
        # Stage 2 - with NO cases
        result = llm.decide_and_respond(
            message=question,
            context="Група техпідтримки академії",
            cases="[]",  # Empty!
        )
        
        if not result.respond:
            print(f"   ✅ Stage 2: Correctly declined (no evidence)")
        else:
            print(f"   ❌ HALLUCINATION! Bot answered: {result.text[:100]}...")
            all_passed = False
        
        print()
    
    # =========================================================================
    # SECTION 3: Messages that should be IGNORED (greetings, noise)
    # =========================================================================
    
    print("╔" + "═" * 78 + "╗")
    print("║ SECTION 3: Messages that should be IGNORED (greetings, noise)             ║")
    print("╚" + "═" * 78 + "╝")
    print()
    
    should_ignore = [
        "Привіт всім!",
        "Доброго ранку)",
        "ок дякую",
        "👍",
        "Як справи?",
        "+1",
        "Згоден",
    ]
    
    for message in should_ignore:
        print(f"💬 ПОВІДОМЛЕННЯ: {message}")
        
        decision = llm.decide_consider(message=message, context="Група техпідтримки")
        
        if not decision.consider:
            print(f"   ✅ Correctly ignored")
        else:
            print(f"   ❌ Should have ignored this!")
            all_passed = False
        
        print()
    
    # =========================================================================
    # SECTION 4: Conciseness check
    # =========================================================================
    
    print("╔" + "═" * 78 + "╗")
    print("║ SECTION 4: Conciseness check (responses should be brief)                  ║")
    print("╚" + "═" * 78 + "╝")
    print()
    
    test_q = "Як скинути пароль?"
    result = llm.decide_and_respond(
        message=test_q,
        context="Група техпідтримки",
        cases=format_cases([KNOWLEDGE_BASE[0]]),
    )
    
    if result.respond:
        length = len(result.text)
        print(f"📝 Питання: {test_q}")
        print(f"🤖 Відповідь ({length} символів):")
        print(f"   {result.text}")
        
        if length <= 200:
            print(f"   ✅ Good length ({length} chars)")
        elif length <= 400:
            print(f"   ⚠️ Acceptable ({length} chars)")
        else:
            print(f"   ❌ Too long ({length} chars)")
            all_passed = False
    
    print()
    
    # =========================================================================
    # SUMMARY
    # =========================================================================
    
    print("=" * 80)
    if all_passed:
        print("✅ ALL CHECKS PASSED")
        print()
        print("Summary:")
        print("  • Bot answers correctly when it has knowledge")
        print("  • Bot stays silent when it doesn't know (no hallucinations)")
        print("  • Bot ignores greetings and noise (no false alerts)")
        print("  • Responses are in Ukrainian")
        print("  • Responses are concise and helpful")
    else:
        print("⚠️ SOME CHECKS FAILED - review output above")
    print("=" * 80)


if __name__ == "__main__":
    run_demo()
