from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

import requests
from bs4 import BeautifulSoup

# Configuration
BASE_URL = "https://community.signalusers.org"
SUPPORT_CATEGORY_ID = 9
OUTPUT_DIR = "../data"
OUTPUT_FILE = "signal_support_dataset.json"
RAW_DIR = "../data/raw"

DEFAULT_MAX_TOPICS = int(os.environ.get("SIGNAL_FORUM_MAX_TOPICS", "2000"))
DEFAULT_MAX_PAGES = int(os.environ.get("SIGNAL_FORUM_MAX_PAGES", "0"))  # 0 => no page cap
DEFAULT_DELAY_SECONDS = float(os.environ.get("SIGNAL_FORUM_DELAY_SECONDS", "0.4"))
DEFAULT_SOURCE = os.environ.get("SIGNAL_FORUM_SOURCE", "latest")  # latest | category
DEFAULT_SUPPORT_ONLY = os.environ.get("SIGNAL_FORUM_SUPPORT_ONLY", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "y",
    "on",
}


def _setup_dirs() -> tuple[str, str]:
    out_dir = os.path.join(os.path.dirname(__file__), OUTPUT_DIR)
    raw_dir = os.path.join(os.path.dirname(__file__), RAW_DIR)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(raw_dir, exist_ok=True)
    return out_dir, raw_dir


def _session() -> requests.Session:
    s = requests.Session()
    # Zendesk/Discourse occasionally blocks requests without UA.
    s.headers.update(
        {
            "User-Agent": "SupportBot-ACL-Benchmark/1.0 (+https://github.com/; research)",
            "Accept": "application/json,text/plain,*/*",
        }
    )
    return s


def _get_json(s: requests.Session, url: str, *, timeout: int = 20) -> Optional[dict]:
    """GET JSON with simple 429 backoff."""
    for attempt in range(6):
        try:
            resp = s.get(url, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                wait = 2**attempt
                print(f"Rate limited (429). Sleeping {wait}s then retrying: {url}")
                time.sleep(wait)
                continue
            print(f"Failed fetch ({resp.status_code}) {url}")
            return None
        except Exception as e:
            print(f"Error fetching {url}: {e}")
            time.sleep(1.0)
    return None


def _clean_cooked_html(cooked: str) -> str:
    """Convert Discourse cooked HTML to readable text."""
    if not cooked:
        return ""

    try:
        soup = BeautifulSoup(cooked, "html.parser")
        # Preserve code blocks reasonably.
        for br in soup.find_all("br"):
            br.replace_with("\n")
        for p in soup.find_all(["p", "li", "blockquote", "pre"]):
            p.append("\n")
        text = soup.get_text(separator=" ", strip=True)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
    except Exception:
        # Fallback: strip tags
        text = re.sub(r"<[^>]+>", " ", cooked)
        return re.sub(r"\s+", " ", text).strip()


def _extract_images(cooked: str) -> List[str]:
    if not cooked:
        return []
    try:
        soup = BeautifulSoup(cooked, "html.parser")
        urls = []
        for img in soup.find_all("img"):
            src = (img.get("src") or "").strip()
            if not src:
                continue
            # Prefer uploads (Discourse CDN paths)
            if "/uploads/" in src:
                urls.append(src)
        return urls
    except Exception:
        img_urls = re.findall(r'<img [^>]*src="([^"]+)"', cooked or "")
        return [u for u in img_urls if "/uploads/" in u]


def _topic_url(slug: str, topic_id: int) -> str:
    return f"{BASE_URL}/t/{slug}/{topic_id}"


def _post_url(slug: str, topic_id: int, post_number: int) -> str:
    # Discourse canonical per-post URL includes the post number at the end.
    return f"{BASE_URL}/t/{slug}/{topic_id}/{post_number}"


def _iter_topics_latest(
    s: requests.Session, *, max_pages: int, delay_seconds: float
) -> Iterable[dict]:
    page = 0
    while True:
        if max_pages and page >= max_pages:
            return
        url = f"{BASE_URL}/latest.json?page={page}"
        print(f"Fetching latest topics page {page}...")
        data = _get_json(s, url)
        if not data:
            return
        topics = (data.get("topic_list") or {}).get("topics") or []
        if not topics:
            return
        for t in topics:
            yield t
        page += 1
        time.sleep(delay_seconds)


def _iter_topics_category(
    s: requests.Session, *, category_id: int, max_pages: int, delay_seconds: float
) -> Iterable[dict]:
    page = 0
    while True:
        if max_pages and page >= max_pages:
            return
        url = f"{BASE_URL}/c/support/{category_id}.json?page={page}"
        print(f"Fetching category={category_id} page {page}...")
        data = _get_json(s, url)
        if not data:
            return
        topics = (data.get("topic_list") or {}).get("topics") or []
        if not topics:
            return
        for t in topics:
            yield t
        page += 1
        time.sleep(delay_seconds)


def _fetch_topic_detail_cached(
    s: requests.Session,
    *,
    raw_dir: str,
    topic_id: int,
    delay_seconds: float,
) -> Optional[dict]:
    raw_path = os.path.join(raw_dir, f"{topic_id}.json")
    if os.path.exists(raw_path):
        try:
            with open(raw_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            # Fall through to refetch.
            pass

    detail = _get_json(s, f"{BASE_URL}/t/{topic_id}.json")
    if detail is not None:
        try:
            with open(raw_path, "w", encoding="utf-8") as f:
                json.dump(detail, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Warning: failed to write raw topic {topic_id}: {e}")
    time.sleep(delay_seconds)
    return detail


def _process_topic(topic_summary: dict, topic_detail: dict) -> Optional[dict]:
    if not topic_detail:
        return None
    post_stream = (topic_detail.get("post_stream") or {}).get("posts") or []
    if not post_stream:
        return None

    topic_id = int(topic_summary.get("id") or topic_detail.get("id") or 0)
    slug = str(topic_summary.get("slug") or topic_detail.get("slug") or "").strip()
    if not topic_id or not slug:
        return None

    # Determine status
    status = "ongoing"
    if topic_summary.get("has_accepted_answer") or (topic_detail.get("accepted_answer") is not None):
        status = "solved"
    elif topic_summary.get("closed"):
        status = "closed"

    accepted_post_number = None
    try:
        accepted_post_number = (topic_detail.get("accepted_answer") or {}).get("post_number")
    except Exception:
        accepted_post_number = None

    processed_messages: List[dict] = []
    for post in post_stream:
        if post.get("post_type") != 1:
            continue

        post_number = int(post.get("post_number") or 0)
        cooked = post.get("cooked") or ""

        actions_summary = post.get("actions_summary") or []
        reactions: List[str] = []
        for action in actions_summary:
            try:
                if int(action.get("count") or 0) > 0:
                    reactions.append(f"action_{action.get('id')}")
            except Exception:
                continue

        is_solution = bool(accepted_post_number and post_number == int(accepted_post_number))

        processed_messages.append(
            {
                "id": str(post.get("id") or ""),
                "post_number": post_number,
                "url": _post_url(slug, topic_id, post_number) if post_number else _topic_url(slug, topic_id),
                "sender_id": post.get("username") or "",
                "content": _clean_cooked_html(cooked),
                "raw_content": cooked,
                "timestamp": post.get("created_at") or "",
                "images": _extract_images(cooked),
                "reactions": reactions,
                "is_solution": is_solution,
            }
        )

    if not processed_messages:
        return None

    return {
        "id": str(topic_id),
        "title": topic_summary.get("title") or topic_detail.get("title") or "",
        "status": status,
        "category_id": int(topic_summary.get("category_id") or topic_detail.get("category_id") or 0),
        "domain": "signal_support_forum",
        "url": _topic_url(slug, topic_id),
        "created_at": topic_summary.get("created_at") or topic_detail.get("created_at") or "",
        "tags": topic_summary.get("tags") or [],
        "messages": processed_messages,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Signal Community forum dataset (Discourse JSON).")
    parser.add_argument("--source", choices=["latest", "category"], default=DEFAULT_SOURCE)
    parser.add_argument("--support-only", action="store_true", default=DEFAULT_SUPPORT_ONLY)
    parser.add_argument("--no-support-only", dest="support_only", action="store_false")
    parser.add_argument("--support-category-id", type=int, default=SUPPORT_CATEGORY_ID)
    parser.add_argument("--max-topics", type=int, default=DEFAULT_MAX_TOPICS)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--delay-seconds", type=float, default=DEFAULT_DELAY_SECONDS)
    args = parser.parse_args()

    out_dir, raw_dir = _setup_dirs()
    s = _session()

    if args.source == "latest":
        topics_iter = _iter_topics_latest(s, max_pages=args.max_pages, delay_seconds=args.delay_seconds)
        print("Using source: /latest.json")
    else:
        topics_iter = _iter_topics_category(
            s,
            category_id=args.support_category_id,
            max_pages=args.max_pages,
            delay_seconds=args.delay_seconds,
        )
        print(f"Using source: /c/support/{args.support_category_id}.json")

    processed_dataset: List[dict] = []
    seen_topic_ids: set[int] = set()

    for i, topic in enumerate(topics_iter, 1):
        if args.max_topics and len(processed_dataset) >= args.max_topics:
            break

        try:
            topic_id = int(topic.get("id") or 0)
        except Exception:
            continue
        if not topic_id or topic_id in seen_topic_ids:
            continue
        seen_topic_ids.add(topic_id)

        # Optional: filter to support category only (default true for this benchmark)
        if args.support_only:
            try:
                if int(topic.get("category_id") or 0) != int(args.support_category_id):
                    continue
            except Exception:
                continue

        title_ascii = str(topic.get("title") or "").encode("ascii", "ignore").decode("ascii")
        print(f"[{i}] topic_id={topic_id} {title_ascii[:120]}")

        detail = _fetch_topic_detail_cached(
            s,
            raw_dir=raw_dir,
            topic_id=topic_id,
            delay_seconds=args.delay_seconds,
        )
        processed = _process_topic(topic, detail or {})
        if processed:
            processed_dataset.append(processed)

    out_path = os.path.join(out_dir, OUTPUT_FILE)
    payload = {
        "meta": {
            "base_url": BASE_URL,
            "source": args.source,
            "support_only": args.support_only,
            "support_category_id": args.support_category_id,
            "max_topics": args.max_topics,
            "max_pages": args.max_pages,
            "delay_seconds": args.delay_seconds,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "topic_count": len(processed_dataset),
            "solved_topic_count": sum(1 for t in processed_dataset if t.get("status") == "solved"),
        },
        "topics": processed_dataset,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    print(f"\nDataset saved to {out_path}")
    print(f"Topics: {payload['meta']['topic_count']}")
    print(f"Solved topics: {payload['meta']['solved_topic_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
