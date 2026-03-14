#!/usr/bin/env python3
"""
FAQ Generator from Signal Messages using Gemini 3 Pro

This script:
1. Loads messages from test/data/signal_messages.json
2. Uses Gemini to cluster and identify common questions/topics
3. Generates a concise FAQ document

Usage:
    python scripts/generate_faq.py

Environment:
    GOOGLE_API_KEY - Your Google API key for Gemini

Output:
    - faq_output.md - Markdown FAQ for Google Docs import
    - faq_output.html - HTML version
"""

import os
import sys
import json
import time
from pathlib import Path
from datetime import datetime
from collections import defaultdict

# Fix encoding for Windows console
if sys.platform == 'win32':
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

# Load .env from repo root
_maybe_load_dotenv(Path(__file__).parent.parent / ".env")
_maybe_load_dotenv(".env")

from google import genai
from google.genai import types

# Configuration
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    raise ValueError("GOOGLE_API_KEY not set. Put it in .env or export it.")

# Initialize the new genai client
client = genai.Client(api_key=GOOGLE_API_KEY)

# Use Gemini 2.0 Flash for good balance of speed and quality
MODEL_NAME = "gemini-2.0-flash"  # Stable model with good context
BATCH_SIZE = 200  # Messages per batch for processing
MAX_BATCHES = 15  # Process up to this many batches

def load_messages(filepath: str) -> list:
    """Load messages from JSON file."""
    print(f"Loading messages from {filepath}...")
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    messages = data.get('messages', [])
    print(f"Loaded {len(messages)} messages")
    return messages

def filter_relevant_messages(messages: list) -> list:
    """Filter out system messages and keep only meaningful content."""
    relevant = []
    for msg in messages:
        # Skip system/change messages
        if msg.get('type') in ['group-v2-change', 'keychange', 'verified-change']:
            continue
        
        body = msg.get('body', '').strip()
        if not body:
            continue
        
        # Skip messages that are only attachments with no text
        if body.startswith('[ATTACHMENT') and '\n' not in body and body.endswith(']'):
            continue
            
        relevant.append({
            'body': body,
            'type': msg.get('type'),
            'timestamp': msg.get('timestamp'),
            'has_attachment': bool(msg.get('attachments'))
        })
    
    print(f"Filtered to {len(relevant)} relevant messages")
    return relevant

def chunk_messages(messages: list, batch_size: int = BATCH_SIZE) -> list:
    """Split messages into batches for processing."""
    batches = []
    for i in range(0, len(messages), batch_size):
        batches.append(messages[i:i + batch_size])
    return batches

def extract_topics_from_batch(batch: list, batch_num: int, global_offset: int) -> str:
    """Use Gemini to extract common topics/questions from a batch of messages."""
    
    # Prepare message text with global message IDs for citation
    messages_text = "\n---\n".join([
        f"[MSG_{global_offset + i + 1}] {msg['body']}" 
        for i, msg in enumerate(batch)
    ])
    
    prompt = f"""Проаналізуй ці повідомлення з чату підтримки (українською/російською про ArduPilot/дрони) та виділи:

1. QUESTIONS: Поширені питання (залиши українською, зберігай технічні терміни)
2. ANSWERS: Рішення/відповіді що були надані (з посиланням на номер повідомлення MSG_XXX)
3. TOPICS: Основні технічні теми

Повідомлення (batch {batch_num}):
{messages_text}

Формат виводу (JSON):
{{
    "questions": [
        {{"q": "Питання 1?", "msg_refs": ["MSG_123", "MSG_456"]}},
        {{"q": "Питання 2?", "msg_refs": ["MSG_789"]}}
    ],
    "answers": [
        {{"question": "Шаблон питання", "answer": "Рішення/Відповідь", "msg_refs": ["MSG_234"]}}
    ],
    "topics": ["Тема 1", "Тема 2"]
}}

Фокус на технічних питаннях про:
- Конфігурацію ArduPilot (параметри, налаштування)
- Налаштування польотного контролера (Mamba, Pixhawk, тощо)
- SBUS/RC конфігурацію
- Проблеми з прошивкою/софтом
- Усунення несправностей апаратного забезпечення

Будь стислим. Поверни тільки валідний JSON."""

    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt
        )
        return response.text
    except Exception as e:
        print(f"Error processing batch {batch_num}: {e}")
        return "{}"

def merge_extracted_data(all_extracts: list) -> dict:
    """Merge extracted data from all batches."""
    merged = {
        'questions': [],
        'answers': [],
        'topics': []
    }
    
    for extract_text in all_extracts:
        try:
            # Clean JSON from markdown code blocks if present
            text = extract_text.strip()
            if text.startswith('```'):
                text = text.split('\n', 1)[1]
            if text.endswith('```'):
                text = text.rsplit('```', 1)[0]
            text = text.strip()
            
            data = json.loads(text)
            
            # Handle new format with citations
            questions = data.get('questions', [])
            for q in questions:
                if isinstance(q, dict):
                    merged['questions'].append(q)
                else:
                    merged['questions'].append({'q': q, 'msg_refs': []})
            
            answers = data.get('answers', [])
            if isinstance(answers, list):
                merged['answers'].extend(answers)
            elif isinstance(answers, dict):
                for k, v in answers.items():
                    merged['answers'].append({'question': k, 'answer': v, 'msg_refs': []})
            
            merged['topics'].extend(data.get('topics', []))
        except (json.JSONDecodeError, Exception) as e:
            print(f"Warning: Could not parse extract: {e}")
            continue
    
    return merged

def count_question_frequency(merged_data: dict) -> dict:
    """Count how many times similar questions appear to identify truly frequent ones."""
    from collections import Counter
    
    # Normalize questions for counting
    question_counts = Counter()
    question_examples = {}
    
    for q in merged_data['questions']:
        q_text = q.get('q', q) if isinstance(q, dict) else q
        if not q_text:
            continue
        # Normalize: lowercase, remove extra spaces
        normalized = ' '.join(q_text.lower().split())
        question_counts[normalized] += 1
        if normalized not in question_examples:
            question_examples[normalized] = q_text
    
    return {
        'counts': question_counts,
        'examples': question_examples
    }

def generate_faq(merged_data: dict, total_messages: int) -> str:
    """Generate final FAQ document from merged data in Ukrainian - clean, no citations."""
    
    # Count question frequency to ensure we only include truly frequent questions
    freq_data = count_question_frequency(merged_data)
    
    # Filter to questions that appear 2+ times (truly frequent)
    frequent_questions = [
        (freq_data['examples'][q], count) 
        for q, count in freq_data['counts'].most_common(50) 
        if count >= 2
    ]
    
    # Format questions with frequency counts for the model
    questions_summary = "\n".join([
        f"- [{count}x] {q}" for q, count in frequent_questions[:40]
    ])
    
    # Format answers (deduplicated)
    seen_answers = set()
    unique_answers = []
    for a in merged_data['answers']:
        if isinstance(a, dict):
            key = (a.get('question', ''), a.get('answer', ''))
            if key not in seen_answers and key[0] and key[1]:
                seen_answers.add(key)
                unique_answers.append(f"- Q: {key[0]} -> A: {key[1]}")
    
    answers_summary = "\n".join(unique_answers[:40])
    topics_summary = ", ".join(set(merged_data['topics']))
    
    prompt = f"""На основі аналізу {total_messages} повідомлень чату підтримки, створи стислий FAQ документ УКРАЇНСЬКОЮ МОВОЮ.

ВАЖЛИВІ ПРАВИЛА:
1. Включай ТІЛЬКИ питання які дійсно часто зустрічаються (позначені [Nx] = N разів)
2. НЕ вигадуй інформацію - використовуй тільки те що є в даних нижче
3. НЕ додавай посилання на джерела типу [MSG_XXX] або [Джерело: ...]
4. Кожна відповідь має базуватися на реальних відповідях з чату

ЧАСТІ ПИТАННЯ (число = скільки разів зустрічалося):
{questions_summary}

ЗНАЙДЕНІ ВІДПОВІДІ:
{answers_summary}

ТЕМИ: {topics_summary}

Створи чистий FAQ документ:
1. 12-18 FAQ записів з НАЙЧАСТІШИХ питань (які зустрічалися 2+ рази)
2. Кожен запис: **Q: питання** та A: стисла відповідь (2-3 речення)
3. Групуй за категоріями (Налаштування, Проблеми, Ліцензування, тощо)
4. Фокус на ArduPilot/StabX технічній підтримці
5. Включай конкретні параметри та значення де є в даних
6. НЕ ДОДАВАЙ посилань на повідомлення - документ має бути чистим

Формат: чистий Markdown.
Почни з:
# FAQ: Підтримка ArduPilot та StabX
*Згенеровано на основі аналізу {total_messages} повідомлень чату підтримки*

Потім категорії з питаннями.

Виведи повний FAQ документ українською мовою."""

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt
    )
    return response.text

def convert_to_html(markdown_text: str) -> str:
    """Simple markdown to HTML conversion for Google Docs."""
    import re
    
    html = markdown_text
    
    # Headers
    html = re.sub(r'^### (.+)$', r'<h3>\1</h3>', html, flags=re.MULTILINE)
    html = re.sub(r'^## (.+)$', r'<h2>\1</h2>', html, flags=re.MULTILINE)
    html = re.sub(r'^# (.+)$', r'<h1>\1</h1>', html, flags=re.MULTILINE)
    
    # Bold and italic
    html = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html)
    html = re.sub(r'\*(.+?)\*', r'<em>\1</em>', html)
    
    # Code
    html = re.sub(r'`([^`]+)`', r'<code>\1</code>', html)
    
    # Links
    html = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', html)
    
    # Line breaks to paragraphs
    paragraphs = html.split('\n\n')
    html = '\n'.join([f'<p>{p}</p>' if not p.startswith('<h') and not p.startswith('<ul') else p for p in paragraphs])
    
    # Wrap in basic HTML
    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>ArduPilot Support FAQ</title>
    <style>
        body {{ font-family: Arial, sans-serif; max-width: 800px; margin: 40px auto; padding: 20px; }}
        h1 {{ color: #1a73e8; }}
        h2 {{ color: #333; border-bottom: 1px solid #ddd; padding-bottom: 5px; }}
        h3 {{ color: #555; }}
        code {{ background: #f5f5f5; padding: 2px 6px; border-radius: 3px; }}
        a {{ color: #1a73e8; }}
    </style>
</head>
<body>
{html}
</body>
</html>"""
    
    return html

def main():
    print("=" * 60)
    print("FAQ Generator from Signal Messages")
    print("=" * 60)
    
    # Find data file
    repo_root = Path(__file__).parent.parent
    data_file = repo_root / "test" / "data" / "signal_messages.json"
    
    if not data_file.exists():
        print(f"ERROR: Data file not found: {data_file}")
        sys.exit(1)
    
    # Load and filter messages
    messages = load_messages(str(data_file))
    relevant = filter_relevant_messages(messages)
    
    # Create batches
    batches = chunk_messages(relevant, BATCH_SIZE)
    batches = batches[:MAX_BATCHES]  # Limit batches
    print(f"Processing {len(batches)} batches of ~{BATCH_SIZE} messages each")
    
    # Show model info
    print(f"\nUsing Gemini model: {MODEL_NAME}")
    
    # Process batches
    all_extracts = []
    global_offset = 0
    for i, batch in enumerate(batches):
        print(f"\nProcessing batch {i+1}/{len(batches)} ({len(batch)} messages)...")
        extract = extract_topics_from_batch(batch, i+1, global_offset)
        all_extracts.append(extract)
        global_offset += len(batch)
        time.sleep(1)  # Rate limiting
    
    # Merge data
    print("\nMerging extracted data...")
    merged = merge_extracted_data(all_extracts)
    print(f"  - Questions: {len(merged['questions'])}")
    print(f"  - Answer patterns: {len(merged['answers'])}")
    print(f"  - Topics: {len(merged['topics'])}")
    
    # Generate FAQ
    print("\nGenerating final FAQ document...")
    faq_markdown = generate_faq(merged, len(relevant))
    
    # Save outputs
    output_dir = repo_root / "docs"
    output_dir.mkdir(exist_ok=True)
    
    md_path = output_dir / "faq_generated.md"
    html_path = output_dir / "faq_generated.html"
    
    print(f"\nSaving FAQ to {md_path}")
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(faq_markdown)
    
    print(f"Saving HTML to {html_path}")
    html_content = convert_to_html(faq_markdown)
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    # Also save raw extracted data for reference
    raw_path = output_dir / "faq_raw_data.json"
    with open(raw_path, 'w', encoding='utf-8') as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    
    print("\n" + "=" * 60)
    print("FAQ Generation Complete!")
    print("=" * 60)
    print(f"\nOutput files:")
    print(f"  - Markdown: {md_path}")
    print(f"  - HTML: {html_path}")
    print(f"  - Raw data: {raw_path}")
    print(f"\nTo import to Google Docs:")
    print(f"  1. Open Google Docs")
    print(f"  2. File > Open > Upload {md_path.name}")
    print(f"  3. Or copy/paste the markdown content")

if __name__ == "__main__":
    main()
