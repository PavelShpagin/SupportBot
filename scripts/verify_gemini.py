#!/usr/bin/env python3
"""
Quick Gemini API health check. Run with: GOOGLE_API_KEY=<key> python3 scripts/verify_gemini.py
"""
import os
import sys

api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("OPENAI_API_KEY")
if not api_key:
    print("ERROR: GOOGLE_API_KEY or OPENAI_API_KEY not set", file=sys.stderr)
    sys.exit(1)

try:
    from openai import OpenAI
except ImportError:
    print("ERROR: pip install openai", file=sys.stderr)
    sys.exit(1)

client = OpenAI(
    api_key=api_key,
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
)
try:
    r = client.chat.completions.create(
        model=os.getenv("MODEL_BLOCKS", "gemini-2.0-flash"),
        messages=[{"role": "user", "content": "Say OK in one word"}],
        max_tokens=5,
    )
    out = (r.choices[0].message.content or "").strip()
    print(f"Gemini API OK: {out}")
except Exception as e:
    print(f"Gemini API ERROR: {e}", file=sys.stderr)
    sys.exit(1)
