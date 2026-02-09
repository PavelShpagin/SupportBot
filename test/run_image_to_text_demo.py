#!/usr/bin/env python3
"""
Run SupportBot's image-to-text extraction on a few decrypted Signal attachments.

Prereqs:
- You have decrypted some images with: python test/decrypt_attachments_v2.py
- GOOGLE_API_KEY is set (recommended: put it in repo-root .env)

Usage:
  source .venv/bin/activate
  python test/run_image_to_text_demo.py

Output (gitignored):
- test/data/image_to_text_samples.json
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List


def _maybe_load_dotenv(dotenv_path: Path) -> None:
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


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    _maybe_load_dotenv(repo / ".env")

    if not os.environ.get("GOOGLE_API_KEY"):
        print("ERROR: GOOGLE_API_KEY is not set (put it in .env or export it).")
        return 2

    # Import SupportBot LLM client
    sys.path.insert(0, str(repo / "signal-bot"))
    from app.config import Settings  # noqa: E402
    from app.llm.client import LLMClient  # noqa: E402

    # Minimal settings (we only use image extraction)
    settings = Settings(
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
        model_img=os.environ.get("MODEL_IMG", "gemini-2.0-flash"),
        model_decision="gemini-2.5-flash-lite",
        model_extract="gemini-2.5-flash-lite",
        model_case="gemini-2.5-flash-lite",
        model_respond="gemini-2.0-flash",
        model_blocks="gemini-2.5-flash-lite",
        embedding_model=os.environ.get("EMBEDDING_MODEL", "gemini-embedding-001"),
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

    llm = LLMClient(settings)

    img_dir = repo / "test" / "data" / "decrypted_attachments" / "image"
    if not img_dir.exists():
        print(f"ERROR: {img_dir} not found. Run: python test/decrypt_attachments_v2.py")
        return 2

    # Pick a few images
    exts = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    images = [p for p in sorted(img_dir.iterdir()) if p.is_file() and p.suffix.lower() in exts]
    if not images:
        print(f"ERROR: No images found in {img_dir}")
        return 2

    n = int(os.environ.get("IMG_DEMO_N", "3"))
    images = images[: max(1, min(n, len(images)))]

    out_path = repo / "test" / "data" / "image_to_text_samples.json"
    results: List[Dict[str, Any]] = []

    print(f"Running image-to-text for {len(images)} image(s) using model {settings.model_img}...")
    for p in images:
        b = p.read_bytes()
        mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
        extracted = llm.image_to_text_json(image_bytes=b, context_text="(support chat attachment)")
        results.append(
            {
                "file": str(p.relative_to(repo)),
                "mime": mime,
                "observations": extracted.observations,
                "extracted_text": extracted.extracted_text,
            }
        )
        text_preview = (extracted.extracted_text or "").strip().replace("\n", " ")
        print(f"- {p.name}: observations={len(extracted.observations)} text_preview={text_preview[:140]!r}")

    out_path.write_text(json.dumps({"results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

