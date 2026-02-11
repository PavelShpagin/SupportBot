#!/usr/bin/env python3
"""
Prepare a streaming evaluation dataset from Signal history.

Pipeline:
1. Load last 500 messages from signal_messages.json
2. Split into:
   - Context KB: first 400 messages -> extract solved cases, embed them
   - Eval set: last 100 messages -> label each with Gemini
3. Labels for eval messages:
   - "ignore": should be ignored (greeting, emoji, irrelevant chatter)
   - "answer": should be answered (question/request that bot can help with)
   - "contains_answer": already contains a solution (bot should stay silent)

Outputs (test/data/streaming_eval/):
- context_kb.json: cached KB from first 400 messages
- eval_messages_labeled.json: labeled last 100 messages
- dataset_meta.json: metadata about the dataset

Usage:
  source .venv/bin/activate
  python test/prepare_streaming_eval_dataset.py

Env vars:
- STREAMING_EVAL_CONTEXT_SIZE=400 (messages for KB)
- STREAMING_EVAL_EVAL_SIZE=100 (messages for evaluation)
- STREAMING_EVAL_TOTAL_SIZE=500 (total messages from end of history)
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from openai import OpenAI


def _now_tag() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _maybe_load_dotenv(dotenv_path: Path) -> None:
    """Load key=value pairs from .env, stripping CRLF."""
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


# --- Label types ---
LabelType = Literal["ignore", "answer", "contains_answer"]


@dataclass
class LabeledMessage:
    idx: int
    timestamp: int
    sender: str
    body: str
    attachments: List[str]  # attachment paths if any
    label: LabelType
    label_reasoning: str
    # For "answer" labels, expected behavior details
    expected_topics: List[str]  # what topics the answer should cover
    knowledge_available: bool  # whether KB has relevant info


@dataclass
class KBCase:
    idx: int
    problem_title: str
    problem_summary: str
    solution_summary: str
    tags: List[str]
    doc_text: str
    embedding: List[float]
    source_messages: List[int]  # timestamps of source messages


# --- Labeling prompt ---
P_LABEL_SYSTEM = """Ти класифікуєш повідомлення з чату підтримки для оцінки бота.

Для кожного повідомлення визнач категорію:
1. "ignore" - бот повинен ігнорувати (привітання, емодзі, нерелевантна балаканина, запитання не по темі)
2. "answer" - бот повинен відповісти (питання/запит на допомогу з технічної теми)
3. "contains_answer" - повідомлення вже містить відповідь/рішення на попереднє питання (бот повинен мовчати)

Поверни ТІЛЬКИ JSON з ключами:
- label: рядок ("ignore", "answer", або "contains_answer")
- reasoning: рядок (1-2 речення чому така класифікація)
- expected_topics: масив рядків (теми які відповідь повинна покрити, тільки для label="answer")
- is_question: boolean (чи це запитання)
- is_solution: boolean (чи це рішення/відповідь на попередню проблему)
"""


P_LABEL_USER = """ПОПЕРЕДНІ ПОВІДОМЛЕННЯ (контекст):
{context}

ПОТОЧНЕ ПОВІДОМЛЕННЯ для класифікації:
sender: {sender}
timestamp: {timestamp}
body: {body}
has_attachments: {has_attachments}

Класифікуй це повідомлення."""


# --- Blocks extraction prompt (same as mine_real_cases.py) ---
P_BLOCKS_SYSTEM = """З довгого фрагменту історії чату витягни вирішені кейси підтримки.
Поверни ТІЛЬКИ JSON з ключем:
- cases: масив об'єктів, кожен з:
  - case_block: рядок (підмножина сирих повідомлень)
  - source_timestamps: масив чисел (timestamps повідомлень у цьому кейсі)
НЕ повертай відкриті/невирішені кейси.

Правила:
- Кожен case_block повинен містити і проблему, і рішення.
- Ігноруй привітання та нерелевантну балаканину.
- Зберігай case_block як точні витяги з фрагменту.
"""


def _chunk_messages(messages: List[Dict[str, Any]], *, max_chars: int, overlap: int) -> List[List[Dict[str, Any]]]:
    """Chunk messages for processing, with overlap."""
    chunks: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    current_len = 0
    
    for m in messages:
        text = (m.get("body") or "").strip()
        msg_len = len(text) + 100  # overhead for sender/timestamp
        
        if current_len + msg_len > max_chars and current:
            chunks.append(current)
            # Keep overlap messages for context
            current = current[-overlap:] if overlap > 0 else []
            current_len = sum(len((x.get("body") or "")) + 100 for x in current)
        
        current.append(m)
        current_len += msg_len
    
    if current:
        chunks.append(current)
    
    return chunks


def _format_messages_for_llm(messages: List[Dict[str, Any]]) -> str:
    """Format messages for LLM context."""
    lines = []
    for m in messages:
        sender = m.get("sender") or "unknown"
        ts = m.get("timestamp") or 0
        body = (m.get("body") or "").strip()
        if body:
            lines.append(f"{sender} ts={ts}\n{body}")
    return "\n\n".join(lines)


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    _maybe_load_dotenv(repo / ".env")
    
    if not os.environ.get("GOOGLE_API_KEY"):
        raise SystemExit("GOOGLE_API_KEY is not set. Put it in .env or export it.")
    
    # Fix embedding model: Gemini OpenAI endpoint requires gemini-embedding-001
    cur_emb = (os.environ.get("EMBEDDING_MODEL") or "").strip()
    if not cur_emb or cur_emb in {"text-embedding-004", "models/text-embedding-004"}:
        os.environ["EMBEDDING_MODEL"] = "gemini-embedding-001"
    
    # Config
    total_size = int(os.environ.get("STREAMING_EVAL_TOTAL_SIZE", "500"))
    context_size = int(os.environ.get("STREAMING_EVAL_CONTEXT_SIZE", "400"))
    eval_size = int(os.environ.get("STREAMING_EVAL_EVAL_SIZE", "100"))
    
    if context_size + eval_size > total_size:
        print(f"Warning: context_size ({context_size}) + eval_size ({eval_size}) > total_size ({total_size})")
        print(f"Adjusting total_size to {context_size + eval_size}")
        total_size = context_size + eval_size
    
    # Output directory
    out_dir = repo / "test" / "data" / "streaming_eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    
    # Load messages
    data_path = repo / "test" / "data" / "signal_messages.json"
    if not data_path.exists():
        raise SystemExit(f"Missing export: {data_path}. Run: python test/read_signal_db.py")
    
    payload = json.loads(data_path.read_text(encoding="utf-8"))
    all_messages: List[Dict[str, Any]] = payload.get("messages") or []
    group_id = payload.get("target_group")
    group_name = payload.get("target_group_name") or ""
    
    if len(all_messages) < total_size:
        print(f"Warning: only {len(all_messages)} messages available, using all")
        total_size = len(all_messages)
        # Adjust splits proportionally
        context_size = int(total_size * 0.8)
        eval_size = total_size - context_size
    
    # Take last N messages
    messages = all_messages[-total_size:]
    context_messages = messages[:context_size]
    eval_messages = messages[context_size:context_size + eval_size]
    
    print(f"Group: {group_name} ({group_id})")
    print(f"Total messages: {len(all_messages)}")
    print(f"Using last {total_size} messages")
    print(f"Context KB: {len(context_messages)} messages")
    print(f"Eval set: {len(eval_messages)} messages")
    
    # Initialize Gemini client
    client = OpenAI(
        api_key=os.environ["GOOGLE_API_KEY"],
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    model = os.environ.get("MODEL_LABELING", "gemini-2.5-flash-lite")
    
    # =====================
    # STEP 1: Build Context KB
    # =====================
    print("\n=== Building Context KB ===")
    
    max_chars = int(os.environ.get("HISTORY_CHUNK_MAX_CHARS", "12000"))
    overlap = int(os.environ.get("HISTORY_CHUNK_OVERLAP_MESSAGES", "3"))
    chunks = _chunk_messages(context_messages, max_chars=max_chars, overlap=overlap)
    print(f"Chunked context into {len(chunks)} chunks")
    
    # Extract cases from context
    case_blocks: List[Dict[str, Any]] = []
    for i, chunk in enumerate(chunks, 1):
        chunk_text = _format_messages_for_llm(chunk)
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": P_BLOCKS_SYSTEM},
                    {"role": "user", "content": f"HISTORY_CHUNK:\n{chunk_text}"},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            raw = resp.choices[0].message.content or "{}"
            data = json.loads(raw)
            cases = data.get("cases", [])
            if isinstance(cases, list):
                for c in cases:
                    if isinstance(c, dict) and isinstance(c.get("case_block"), str) and c["case_block"].strip():
                        case_blocks.append({
                            "case_block": c["case_block"].strip(),
                            "source_timestamps": c.get("source_timestamps", []),
                        })
            print(f"Chunk {i}/{len(chunks)}: extracted {len(cases)} cases (total: {len(case_blocks)})")
        except Exception as e:
            print(f"Chunk {i}/{len(chunks)}: ERROR - {e}")
    
    # Deduplicate case blocks
    seen = set()
    unique_blocks = []
    for cb in case_blocks:
        block_text = cb["case_block"]
        if block_text not in seen:
            seen.add(block_text)
            unique_blocks.append(cb)
    case_blocks = unique_blocks
    print(f"Deduplicated to {len(case_blocks)} unique case blocks")
    
    # Structure cases and embed them using SupportBot's LLM client
    sys.path.insert(0, str(repo / "signal-bot"))
    from app.config import load_settings
    from app.llm.client import LLMClient
    
    settings = load_settings()
    llm = LLMClient(settings)
    
    kb_cases: List[Dict[str, Any]] = []
    skipped_open = 0
    skipped_no_solution = 0
    for idx, cb in enumerate(case_blocks, 1):
        try:
            case = llm.make_case(case_block_text=cb["case_block"])
            if not case.keep:
                continue
            
            # FILTER: Only include SOLVED cases in KB
            if case.status != "solved":
                skipped_open += 1
                continue
            
            # Reject solved cases without solutions (quality gate)
            if not case.solution_summary.strip():
                print(f"Case {idx}: Rejecting solved case without solution_summary")
                skipped_no_solution += 1
                continue
            
            # Build doc_text with clear section labels (only solved cases reach here)
            doc_text = "\n".join([
                f"[SOLVED] {(case.problem_title or '').strip()}",
                f"Проблема: {(case.problem_summary or '').strip()}",
                f"Рішення: {(case.solution_summary or '').strip()}",
                "tags: " + ", ".join(case.tags or []),
            ]).strip()
            
            embedding = llm.embed(text=doc_text) if doc_text else []
            
            kb_cases.append({
                "idx": len(kb_cases) + 1,
                "problem_title": case.problem_title,
                "problem_summary": case.problem_summary,
                "solution_summary": case.solution_summary,
                "status": case.status,
                "tags": case.tags,
                "doc_text": doc_text,
                "embedding": embedding,
                "source_timestamps": cb.get("source_timestamps", []),
                "case_block": cb["case_block"],
            })
            
            if len(kb_cases) % 5 == 0:
                print(f"Processed {len(kb_cases)} KB cases...")
        except Exception as e:
            print(f"Case {idx}: ERROR - {e}")
    
    print(f"Built KB with {len(kb_cases)} SOLVED cases (skipped: {skipped_open} open, {skipped_no_solution} no solution)")
    
    # Save KB
    kb_path = out_dir / "context_kb.json"
    kb_path.write_text(json.dumps({
        "group_id": group_id,
        "group_name": group_name,
        "context_message_count": len(context_messages),
        "created_at": _now_tag(),
        "cases": kb_cases,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved KB: {kb_path}")
    
    # =====================
    # STEP 2: Label Eval Messages
    # =====================
    print("\n=== Labeling Eval Messages ===")
    
    labeled_messages: List[Dict[str, Any]] = []
    
    # Build rolling context window for labeling
    context_window_size = 10  # Look at last 10 messages for context
    
    for i, msg in enumerate(eval_messages):
        # Get context: last N messages before this one
        start_ctx = max(0, i - context_window_size)
        context_msgs = eval_messages[start_ctx:i]
        # Also include last few from context_messages if at the beginning
        if i < context_window_size:
            prepend_count = context_window_size - i
            context_msgs = context_messages[-prepend_count:] + context_msgs
        
        context_text = _format_messages_for_llm(context_msgs) if context_msgs else "(no previous messages)"
        
        sender = msg.get("sender") or "unknown"
        timestamp = msg.get("timestamp") or 0
        body = (msg.get("body") or "").strip()
        attachments = msg.get("attachments") or []
        has_attachments = len(attachments) > 0
        
        # Skip empty messages
        if not body and not has_attachments:
            continue
        
        try:
            user_prompt = P_LABEL_USER.format(
                context=context_text,
                sender=sender,
                timestamp=timestamp,
                body=body if body else "(no text, only attachment)",
                has_attachments=str(has_attachments).lower(),
            )
            
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": P_LABEL_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            raw = resp.choices[0].message.content or "{}"
            data = json.loads(raw)
            
            label = data.get("label", "ignore")
            if label not in ("ignore", "answer", "contains_answer"):
                label = "ignore"
            
            labeled_messages.append({
                "idx": len(labeled_messages) + 1,
                "timestamp": timestamp,
                "sender": sender,
                "body": body,
                "attachments": [str(a) for a in attachments],
                "label": label,
                "label_reasoning": data.get("reasoning", ""),
                "expected_topics": data.get("expected_topics", []),
                "is_question": data.get("is_question", False),
                "is_solution": data.get("is_solution", False),
            })
            
            if len(labeled_messages) % 10 == 0:
                print(f"Labeled {len(labeled_messages)} messages...")
                
        except Exception as e:
            print(f"Message {i+1}: ERROR - {e}")
            # Still add with default label
            labeled_messages.append({
                "idx": len(labeled_messages) + 1,
                "timestamp": timestamp,
                "sender": sender,
                "body": body,
                "attachments": [str(a) for a in attachments],
                "label": "ignore",
                "label_reasoning": f"Error during labeling: {e}",
                "expected_topics": [],
                "is_question": False,
                "is_solution": False,
            })
    
    # Count labels
    label_counts = {"ignore": 0, "answer": 0, "contains_answer": 0}
    for m in labeled_messages:
        label_counts[m["label"]] += 1
    
    print(f"\nLabel distribution:")
    print(f"  ignore: {label_counts['ignore']}")
    print(f"  answer: {label_counts['answer']}")
    print(f"  contains_answer: {label_counts['contains_answer']}")
    
    # Save labeled messages
    eval_path = out_dir / "eval_messages_labeled.json"
    eval_path.write_text(json.dumps({
        "group_id": group_id,
        "group_name": group_name,
        "eval_message_count": len(labeled_messages),
        "label_counts": label_counts,
        "created_at": _now_tag(),
        "messages": labeled_messages,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved labeled eval messages: {eval_path}")
    
    # Save metadata
    meta_path = out_dir / "dataset_meta.json"
    meta_path.write_text(json.dumps({
        "group_id": group_id,
        "group_name": group_name,
        "created_at": _now_tag(),
        "total_messages_used": total_size,
        "context_message_count": len(context_messages),
        "eval_message_count": len(labeled_messages),
        "kb_case_count": len(kb_cases),
        "label_counts": label_counts,
        "model_used": model,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved metadata: {meta_path}")
    
    print("\n=== Dataset Preparation Complete ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
