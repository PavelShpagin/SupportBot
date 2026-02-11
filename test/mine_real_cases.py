#!/usr/bin/env python3
"""
Mine solved support cases from real Signal group history export.

Pipeline (mirrors production history ingest):
1) Load exported group messages from test/data/signal_messages.json
2) Chunk history text
3) Use Gemini (OpenAI-compatible API) to extract solved case blocks
4) Use SupportBot LLM prompts to normalize each case block into a structured case
5) Compute embeddings for retrieval and write outputs to test/data/

Outputs (gitignored under test/data/):
- signal_case_blocks.json
- signal_cases_structured.json

Usage (WSL):
  # Put GOOGLE_API_KEY=... in .env (repo root), or export it in your shell
  source .venv/bin/activate
  python test/mine_real_cases.py

Optional env vars:
- REAL_LAST_N_MESSAGES: use only the last N messages from the export (e.g. 800)
- REAL_MESSAGES_PATH: override input export path (default: test/data/signal_messages.json)
- REAL_OUT_DIR: write outputs into this directory (default: test/data/)
- REAL_MAX_CASES: stop after keeping this many structured cases (default: 60)
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from openai import OpenAI


P_BLOCKS_SYSTEM = """З довгого фрагменту історії чату витягни вирішені кейси підтримки.
Поверни ТІЛЬКИ JSON з ключем:
- cases: масив об'єктів, кожен з:
  - case_block: рядок (підмножина сирих повідомлень)
НЕ повертай відкриті/невирішені кейси.

Правила:
- Кожен case_block повинен містити і проблему, і рішення.
- Ігноруй привітання та нерелевантну балаканину.
- Зберігай case_block як точні витяги з фрагменту.
"""


def _load_env_hint() -> None:
    if not os.environ.get("GOOGLE_API_KEY"):
        raise SystemExit("GOOGLE_API_KEY is not set. Put it in .env or export it in the environment.")


def _maybe_load_dotenv(dotenv_path: Path) -> None:
    """
    Load key=value pairs from .env, stripping CRLF.

    We *don't* rely on `source .env` because many Windows checkouts have CRLF,
    which can leak '\\r' into values (breaking HTTP headers).
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
        # Strip optional quotes
        if (v.startswith("'") and v.endswith("'")) or (v.startswith('"') and v.endswith('"')):
            v = v[1:-1]
        # Do not override existing env
        os.environ.setdefault(k, v)


def _chunk_lines(lines: list[str], *, max_chars: int, overlap_messages: int) -> list[str]:
    chunks: list[str] = []
    cur: list[str] = []
    for line in lines:
        candidate = "".join(cur) + line
        if len(candidate) > max_chars and cur:
            chunks.append("".join(cur))
            cur = cur[-overlap_messages:] if overlap_messages > 0 else []
        cur.append(line)
    if cur:
        chunks.append("".join(cur))
    return chunks


def _extract_image_observations(case_block: str, llm, repo: Path, enable_image_processing: bool = True) -> str:
    """
    Enhance case_block with image context markers to improve retrieval.
    Detects images and adds surrounding text context for better embedding.
    """
    if not enable_image_processing:
        return case_block
    
    # Check if block has any image attachments
    if '[ATTACHMENT image' not in case_block and '[ATTACHMENT application/pdf' not in case_block:
        return case_block
    
    # Extract surrounding context to understand what the image/PDF is about
    lines = case_block.split('\n')
    contexts = []
    
    for i, line in enumerate(lines):
        if '[ATTACHMENT' in line:
            # Get text BEFORE attachment (what user said when sending image)
            text_before = []
            j = i - 1
            while j >= 0 and len(text_before) < 3:
                line_clean = lines[j].strip()
                # Skip headers (sender ts=...)
                if not re.match(r'^[a-f0-9-]+\s+ts=\d+', line_clean) and line_clean:
                    text_before.insert(0, line_clean)
                j -= 1
            
            # Get text AFTER attachment (user's follow-up or explanation)
            text_after = []
            j = i + 1
            while j < len(lines) and len(text_after) < 3:
                line_clean = lines[j].strip()
                # Skip headers
                if not re.match(r'^[a-f0-9-]+\s+ts=\d+', line_clean) and line_clean:
                    text_after.append(line_clean)
                    # Stop if we hit next sender
                    if j + 1 < len(lines) and re.match(r'^[a-f0-9-]+\s+ts=\d+', lines[j + 1].strip()):
                        break
                j += 1
            
            # Combine context
            ctx_parts = text_before + text_after
            if ctx_parts:
                ctx_text = ' '.join(ctx_parts)
                if len(ctx_text) > 15:  # Meaningful text only
                    contexts.append(ctx_text[:300])
    
    # Add enhancement if we found context
    if contexts:
        # Deduplicate
        unique_contexts = list(dict.fromkeys(contexts))
        enhancement = f"\n\n[Візуальні матеріали: {'; '.join(unique_contexts)}]"
        return case_block + enhancement
    
    return case_block


def main() -> None:
    repo = Path(__file__).parent.parent
    _maybe_load_dotenv(repo / ".env")
    _load_env_hint()

    out_dir_raw = (os.environ.get("REAL_OUT_DIR") or "").strip()
    out_dir = Path(out_dir_raw) if out_dir_raw else (repo / "test" / "data")
    out_dir.mkdir(parents=True, exist_ok=True)

    data_path_raw = (os.environ.get("REAL_MESSAGES_PATH") or "").strip()
    data_path = Path(data_path_raw) if data_path_raw else (repo / "test" / "data" / "signal_messages.json")
    if not data_path.exists():
        raise SystemExit(f"Missing export: {data_path}. Run: python test/read_signal_db.py")

    payload = json.loads(data_path.read_text(encoding="utf-8"))
    msgs: list[dict[str, Any]] = payload.get("messages") or []
    group_id = payload.get("target_group")
    group_name = payload.get("target_group_name") or ""

    last_n = int(os.environ.get("REAL_LAST_N_MESSAGES", "0") or "0")
    if last_n > 0 and len(msgs) > last_n:
        msgs = msgs[-last_n:]

    if not msgs:
        raise SystemExit("No messages found in export.")

    print(f"Loaded messages: {len(msgs)}", flush=True)
    print(f"Group: {group_name} ({group_id})", flush=True)
    if last_n > 0:
        print(f"Subset: last {last_n} messages", flush=True)

    # Format like production history chunking does.
    lines: list[str] = []
    for m in msgs:
        text = (m.get("body") or "").strip()
        if not text:
            continue
        sender = m.get("sender") or "unknown"
        ts = m.get("timestamp") or 0
        lines.append(f"{sender} ts={ts}\n{text}\n\n")

    max_chars = int(os.environ.get("HISTORY_CHUNK_MAX_CHARS", "12000"))
    overlap = int(os.environ.get("HISTORY_CHUNK_OVERLAP_MESSAGES", "3"))
    chunks = _chunk_lines(lines, max_chars=max_chars, overlap_messages=overlap)
    print(f"Chunked history: {len(chunks)} chunks (max_chars={max_chars}, overlap={overlap})", flush=True)

    # Gemini OpenAI-compatible client
    client = OpenAI(
        api_key=os.environ["GOOGLE_API_KEY"],
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    model_blocks = os.environ.get("MODEL_BLOCKS", "gemini-2.5-flash-lite")

    out_blocks = out_dir / "signal_case_blocks.json"
    reuse_blocks = os.environ.get("REAL_REUSE_BLOCKS", "1").strip() not in {"0", "false", "no"}

    if reuse_blocks and out_blocks.exists():
        cached = json.loads(out_blocks.read_text(encoding="utf-8"))
        case_blocks = cached.get("case_blocks") or []
        if not isinstance(case_blocks, list):
            case_blocks = []
        case_blocks = [str(x).strip() for x in case_blocks if str(x).strip()]
        print(f"Reusing cached case blocks: {len(case_blocks)} ({out_blocks})", flush=True)
    else:
        case_blocks: list[str] = []
        for i, ch in enumerate(chunks, 1):
            resp = client.chat.completions.create(
                model=model_blocks,
                messages=[
                    {"role": "system", "content": P_BLOCKS_SYSTEM},
                    {"role": "user", "content": f"HISTORY_CHUNK:\n{ch}"},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            raw = resp.choices[0].message.content or "{}"
            try:
                data = json.loads(raw)
            except Exception:
                print(f"WARNING: chunk {i}: invalid JSON from model_blocks; skipping", flush=True)
                continue
            cases = data.get("cases", [])
            if isinstance(cases, list):
                for c in cases:
                    if isinstance(c, dict) and isinstance(c.get("case_block"), str) and c["case_block"].strip():
                        case_blocks.append(c["case_block"].strip())
            print(f"chunk {i}/{len(chunks)}: extracted blocks so far: {len(case_blocks)}", flush=True)

        # Dedup while preserving order
        case_blocks = list(dict.fromkeys(case_blocks))
        print(f"Deduped case blocks: {len(case_blocks)}", flush=True)

        out_blocks.write_text(
            json.dumps({"group_id": group_id, "group_name": group_name, "case_blocks": case_blocks}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Wrote case blocks: {out_blocks}", flush=True)

    # Use SupportBot's structuring prompt (make_case) + embedding
    sys.path.insert(0, str(repo / "signal-bot"))
    from app.config import load_settings  # noqa: E402
    from app.llm.client import LLMClient  # noqa: E402

    settings = load_settings()
    llm = LLMClient(settings)

    structured: list[dict[str, Any]] = []
    kept = 0
    images_processed = 0
    for idx, block in enumerate(case_blocks, 1):
        # Enhance case_block with image context if images present
        has_img = '[ATTACHMENT' in block
        enhanced_block = _extract_image_observations(block, llm, repo)
        if has_img and enhanced_block != block:
            images_processed += 1
            print(f"Block {idx}: Enhanced with image context", flush=True)
        
        case = llm.make_case(case_block_text=enhanced_block)
        if not case.keep:
            continue

        # Quality gate: Only keep solved cases with solutions
        # Reject: solved cases without solutions OR open/unsolved cases
        if case.status != "solved" or not case.solution_summary.strip():
            print(f"Block {idx}: Rejecting case (status={case.status}, has_solution={bool(case.solution_summary.strip())})", flush=True)
            continue

        # Build doc_text with clear section labels
        # Use enhanced_block for better context
        solution_text = (case.solution_summary or "").strip()
        if case.status == "solved" and solution_text:
            doc_text = "\n".join([
                f"[{case.status.upper()}] {(case.problem_title or '').strip()}",
                f"Проблема: {(case.problem_summary or '').strip()}",
                f"Рішення: {solution_text}",
                "tags: " + ", ".join(case.tags or []),
            ]).strip()
        else:
            doc_text = "\n".join([
                f"[{(case.status or 'open').upper()}] {(case.problem_title or '').strip()}",
                f"Проблема: {(case.problem_summary or '').strip()}",
                "tags: " + ", ".join(case.tags or []),
            ]).strip()
        try:
            emb = llm.embed(text=doc_text) if doc_text else []
        except Exception as e:
            raise RuntimeError(
                "Embedding failed. If you're using the Gemini OpenAI endpoint, "
                "set EMBEDDING_MODEL=gemini-embedding-001 (text-embedding-004 is often unsupported)."
            ) from e

        kept += 1
        structured.append(
            {
                "idx": kept,
                "problem_title": case.problem_title,
                "problem_summary": case.problem_summary,
                "solution_summary": case.solution_summary,
                "status": case.status,
                "tags": case.tags,
                "evidence_ids": case.evidence_ids,
                "doc_text": doc_text,
                "embedding": emb,
                "case_block": enhanced_block,  # Store enhanced version with image context
            }
        )

        if kept % 10 == 0:
            print(f"Structured kept cases: {kept}/{idx} blocks", flush=True)

        # Avoid runaway costs by default; can override with env var.
        max_keep = int(os.environ.get("REAL_MAX_CASES", "60"))
        if kept >= max_keep:
            break

    out_struct = out_dir / "signal_cases_structured.json"
    out_struct.write_text(
        json.dumps(
            {
                "group_id": group_id,
                "group_name": group_name,
                "model_blocks": model_blocks,
                "source_messages_path": str(data_path),
                "source_messages_used": len(msgs),
                "kept_cases": kept,
                "images_processed": images_processed,
                "cases": structured,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote structured cases: {out_struct}", flush=True)
    print(f"Kept structured cases: {kept}", flush=True)
    print(f"Images processed: {images_processed}", flush=True)


if __name__ == "__main__":
    main()

