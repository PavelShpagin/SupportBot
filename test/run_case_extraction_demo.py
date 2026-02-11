#!/usr/bin/env python3
"""
Case extraction demonstration - shows how the bot extracts cases from chat history.

Run (recommended):
  - Put `GOOGLE_API_KEY=...` in `.env` (repo root), OR export it in your shell
  - `python test/run_case_extraction_demo.py`
"""

import json
import os
import sys
from pathlib import Path

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
    print("ERROR: GOOGLE_API_KEY not set")
    sys.exit(1)

from app.llm.client import LLMClient
from app.config import Settings


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
        buffer_max_age_hours=168,
        buffer_max_messages=1000,
        max_images_per_gate=3,
        max_images_per_respond=5,
        max_kb_images_per_case=2,
        max_image_size_bytes=5_000_000,
        max_total_image_bytes=20_000_000,
    )


# Realistic Ukrainian chat buffer with solved cases
CHAT_BUFFER = """
user_abc123 ts=1707400000000
Привіт! Не можу зайти в особистий кабінет, пише 'невірний пароль' хоча пароль точно правильний

support_xyz ts=1707400060000
Вітаю! Спробуйте очистити кеш браузера та cookies. Також перевірте чи не увімкнений Caps Lock

user_abc123 ts=1707400120000
Кеш почистив, не допомогло

support_xyz ts=1707400180000
Тоді спробуйте скинути пароль через форму відновлення на сторінці входу. Лист прийде на вашу пошту

user_abc123 ts=1707400300000
Скинув пароль, тепер все працює! Дякую!

support_xyz ts=1707400360000
Радий що допомогло! Якщо будуть питання - звертайтесь

user_def456 ts=1707401000000
Добрий день, відео уроки не завантажуються, крутиться колесо і все

support_qwe ts=1707401060000
Доброго дня! Який браузер використовуєте?

user_def456 ts=1707401120000
Firefox

support_qwe ts=1707401180000
Спробуйте в Chrome або Edge. У Firefox іноді бувають проблеми з нашим плеєром

user_def456 ts=1707401300000
В Chrome запрацювало, дякую!

user_ghi789 ts=1707402000000
Привіт всім)

user_jkl012 ts=1707402010000
Привіт!

user_mno345 ts=1707403000000
Оплатив курс але доступ не з'явився, гроші списались з картки

support_xyz ts=1707403060000
Вкажіть, будь ласка, номер транзакції або email на який оформлювали

user_mno345 ts=1707403120000
Email: user@gmail.com, транзакція #TRX-2024-8847

support_xyz ts=1707403180000
Знайшов вашу оплату. Був технічний збій, зараз активую доступ вручну. Зачекайте 5 хвилин і оновіть сторінку

user_mno345 ts=1707403300000
Доступ з'явився, все працює. Дякую за швидку допомогу!
"""


def run_demo():
    print("=" * 80)
    print("CASE EXTRACTION DEMONSTRATION")
    print("=" * 80)
    print()
    print("This shows how SupportBot extracts solved cases from chat history")
    print("to build its knowledge base.")
    print()
    
    settings = create_settings()
    llm = LLMClient(settings)
    
    print("╔" + "═" * 78 + "╗")
    print("║ INPUT: Raw chat buffer                                                     ║")
    print("╚" + "═" * 78 + "╝")
    print()
    print(CHAT_BUFFER[:1000] + "..." if len(CHAT_BUFFER) > 1000 else CHAT_BUFFER)
    print()
    
    # =========================================================================
    # Step 1: Extract case from buffer
    # =========================================================================
    
    print("╔" + "═" * 78 + "╗")
    print("║ STEP 1: Extract solved case from buffer                                    ║")
    print("╚" + "═" * 78 + "╝")
    print()
    
    extract_result = llm.extract_case_from_buffer(buffer_text=CHAT_BUFFER)
    
    print(f"Found cases: {len(extract_result.cases)}")
    if extract_result.cases:
        first_case = extract_result.cases[0]
        print()
        print("Extracted case block:")
        print("-" * 40)
        print(first_case.case_block[:500])
        print("-" * 40)
    print()
    
    # =========================================================================
    # Step 2: Structure the case
    # =========================================================================
    
    if extract_result.cases:
        print("╔" + "═" * 78 + "╗")
        print("║ STEP 2: Structure the case for knowledge base                             ║")
        print("╚" + "═" * 78 + "╝")
        print()
        
        first_case = extract_result.cases[0]
        case_result = llm.make_case(case_block_text=first_case.case_block)
        
        print(f"Keep: {case_result.keep}")
        print(f"Status: {case_result.status}")
        print()
        print(f"📌 Problem Title: {case_result.problem_title}")
        print()
        print(f"📋 Problem Summary:")
        print(f"   {case_result.problem_summary}")
        print()
        print(f"✅ Solution Summary:")
        print(f"   {case_result.solution_summary}")
        print()
        print(f"🏷️ Tags: {', '.join(case_result.tags)}")
        print()
        
        # Show how it would be stored for RAG
        doc_text = "\n".join([
            case_result.problem_title.strip(),
            case_result.problem_summary.strip(),
            case_result.solution_summary.strip(),
            "tags: " + ", ".join(case_result.tags),
        ]).strip()
        
        print("╔" + "═" * 78 + "╗")
        print("║ STEP 3: Document stored in vector database (for RAG retrieval)            ║")
        print("╚" + "═" * 78 + "╝")
        print()
        print(doc_text)
        print()
    
    # =========================================================================
    # Step 3: Continue extraction (get more cases)
    # =========================================================================
    
    if extract_result.cases:
        # Demo deterministic trim by indexes from the first extracted case.
        start_idx = extract_result.cases[0].start_idx
        end_idx = extract_result.cases[0].end_idx
        raw_blocks = [b for b in CHAT_BUFFER.split("\n\n") if b.strip()]
        remaining_blocks = [b for i, b in enumerate(raw_blocks) if i < start_idx or i > end_idx]
        remaining_buffer = "\n\n".join(remaining_blocks).strip()
        if remaining_buffer:
            remaining_buffer += "\n\n"
        else:
            remaining_buffer = ""

        print("╔" + "═" * 78 + "╗")
        print("║ STEP 4: Extract next case from remaining buffer                           ║")
        print("╚" + "═" * 78 + "╝")
        print()
        
        extract_result2 = llm.extract_case_from_buffer(buffer_text=remaining_buffer)
        
        print(f"Found another case: {len(extract_result2.cases) > 0}")
        
        if extract_result2.cases:
            case_result2 = llm.make_case(case_block_text=extract_result2.cases[0].case_block)
            
            if case_result2.keep:
                print()
                print(f"📌 Problem Title: {case_result2.problem_title}")
                print(f"✅ Solution: {case_result2.solution_summary[:100]}...")
                print(f"🏷️ Tags: {', '.join(case_result2.tags)}")
    
    print()
    
    # =========================================================================
    # Step 4: Test rejection of greetings
    # =========================================================================
    
    print("╔" + "═" * 78 + "╗")
    print("║ STEP 5: Verify greetings are NOT extracted as cases                        ║")
    print("╚" + "═" * 78 + "╝")
    print()
    
    greeting_buffer = """
user_abc ts=1707402000000
Привіт всім)

user_def ts=1707402010000
Привіт!

user_ghi ts=1707402020000
Як справи?

user_jkl ts=1707402030000
Нормально, а в тебе?
"""
    
    greeting_extract = llm.extract_case_from_buffer(buffer_text=greeting_buffer)
    
    if not greeting_extract.cases:
        print("✅ Correctly: No case extracted from greetings")
    else:
        print("⚠️ Warning: Case extracted from greetings (checking if kept...)")
        case = llm.make_case(case_block_text=greeting_extract.cases[0].case_block)
        if not case.keep:
            print("✅ Correctly: Case rejected at structuring step")
        else:
            print("❌ Error: Greeting kept as case!")
    
    print()
    print("=" * 80)
    print("CASE EXTRACTION COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    run_demo()
