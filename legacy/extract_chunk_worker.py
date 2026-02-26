#!/usr/bin/env python3
"""
Worker: extract cases from a single chunk. Run in subprocess to avoid
Gemini API hang on 2nd consecutive request from same process.
Usage: python3 extract_chunk_worker.py <chunk_file.json>
Input: JSON file with {"chunk_text": str, "api_key": str, "model": str, "structured": bool}
When structured=True: output list of structured case dicts. Else: list of case_block strings.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
for d in [str(REPO_ROOT / "signal-ingest"), str(REPO_ROOT / "signal-bot")]:
    if d not in sys.path:
        sys.path.insert(0, d)

from openai import OpenAI
from ingest.main import _extract_case_blocks, _extract_structured_cases


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: extract_chunk_worker.py <chunk_file.json>")
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        data = json.load(f)
    chunk_text = data["chunk_text"]
    api_key = data["api_key"]
    model = data["model"]
    structured = data.get("structured", False)
    client = OpenAI(
        api_key=api_key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        timeout=120.0,
    )
    if structured:
        cases = _extract_structured_cases(openai_client=client, model=model, chunk_text=chunk_text)
        json.dump(cases, sys.stdout, ensure_ascii=False)
    else:
        blocks = _extract_case_blocks(openai_client=client, model=model, chunk_text=chunk_text)
        json.dump(blocks, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
