from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any, Dict, List


def _load_topics(payload: Any) -> tuple[dict, List[dict]]:
    """
    Accepts either:
    - old format: [topic, topic, ...]
    - new format: {"meta": {...}, "topics": [topic, ...]}
    """
    if isinstance(payload, list):
        return {}, payload
    if isinstance(payload, dict):
        meta = payload.get("meta") or {}
        topics = payload.get("topics") or []
        if isinstance(topics, list):
            return meta, topics
    return {}, []


def convert_to_eval_format(input_file: str, output_file: str) -> None:
    with open(input_file, "r", encoding="utf-8") as f:
        payload = json.load(f)

    source_meta, topics = _load_topics(payload)
    eval_cases: List[dict] = []

    for topic in topics:
        messages = topic.get("messages") or []
        if not messages:
            continue

        # First user post acts as the "query"
        first_msg = messages[0]
        if not (first_msg.get("content") or "").strip():
            continue

        # Accepted-answer post if present
        solution_msg = next((m for m in messages if m.get("is_solution")), None)

        label = "unknown"
        if solution_msg and topic.get("status") == "solved":
            label = "answer"

        case = {
            "id": topic.get("id"),
            "domain": topic.get("domain") or "signal_support_forum",
            "source": "community.signalusers.org",
            "category_id": topic.get("category_id"),
            "tags": topic.get("tags") or [],
            "context": {
                "thread_title": topic.get("title"),
                "thread_url": topic.get("url"),
                "created_at": topic.get("created_at"),
                "status": topic.get("status"),
            },
            "input_message": {
                "sender": first_msg.get("sender_id"),
                "content": first_msg.get("content"),
                "timestamp": first_msg.get("timestamp"),
                "post_number": first_msg.get("post_number"),
                "url": first_msg.get("url"),
                "images": first_msg.get("images") or [],
            },
            "expected_output": {
                "label": label,
                "has_solution": bool(solution_msg),
                "solution_content": solution_msg.get("content") if solution_msg else None,
                "solution_author": solution_msg.get("sender_id") if solution_msg else None,
                "solution_post_number": solution_msg.get("post_number") if solution_msg else None,
                "solution_url": solution_msg.get("url") if solution_msg else None,
            },
            "full_thread": [
                {
                    "sender": m.get("sender_id"),
                    "content": m.get("content"),
                    "timestamp": m.get("timestamp"),
                    "post_number": m.get("post_number"),
                    "url": m.get("url"),
                    "is_solution": bool(m.get("is_solution")),
                }
                for m in messages
            ],
        }
        eval_cases.append(case)

    out_payload = {
        "meta": {
            **(source_meta or {}),
            "processed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "case_count": len(eval_cases),
            "solved_case_count": sum(1 for c in eval_cases if c["expected_output"]["label"] == "answer"),
        },
        "cases": eval_cases,
    }

    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(out_payload, f, indent=2, ensure_ascii=False)

    print(f"Converted {len(eval_cases)} topics into eval cases.")
    print(f"Solved eval cases: {out_payload['meta']['solved_case_count']}")
    print(f"Saved to {output_file}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert fetched forum topics into eval-case JSON.")
    parser.add_argument("--input", default="../data/signal_support_dataset.json")
    parser.add_argument("--output", default="../data/signal_eval_dataset.json")
    args = parser.parse_args()

    script_dir = os.path.dirname(__file__)
    abs_input = os.path.join(script_dir, args.input)
    abs_output = os.path.join(script_dir, args.output)
    convert_to_eval_format(abs_input, abs_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
