#!/usr/bin/env python3
"""
Test media (image) support in the history ingest pipeline.

Usage:
    python test_media_ingest.py [--url https://supportbot.info] [--group-id GROUP_ID]

What it does:
1. Creates a debug history token via /history/token
2. Posts a synthetic case + 2 messages (one with an image attachment) to /history/cases
3. Fetches the resulting case page and verifies the image URL is present
4. Prints a table of all cases for the group with links

Requires HTTP_DEBUG_ENDPOINTS_ENABLED=true on the target server.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import struct
import time
import zlib
import sys
import urllib.request
import urllib.parse

BASE_URL = "https://supportbot.info"
# Real group — use only when you accept that existing cases will be archived+replaced.
REAL_GROUP_ID = "1fWBz1RwCF0B/wGHfNMER4NkWBJPYvjGCv2kXsBJTok="
# Synthetic group ID for safe testing (bot cannot be "in" it, but the debug token
# bypass allows ingest anyway when signal verification fails gracefully).
TEST_GROUP_ID = "test-group-media-verification-only"
GROUP_ID = TEST_GROUP_ID  # default to safe test group
ADMIN_ID = "test_media_admin"


def _post(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _get(url: str) -> bytes:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read()


def _make_test_png(width: int = 40, height: int = 40, r: int = 70, g: int = 130, b: int = 180) -> bytes:
    """Create a minimal valid PNG image with a solid colour (no PIL needed)."""
    def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
        length = struct.pack(">I", len(data))
        crc = struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
        return length + chunk_type + data + crc

    raw_rows = b""
    for _ in range(height):
        row = bytes([0])  # filter type = None
        for _ in range(width):
            row += bytes([r, g, b])
        raw_rows += row

    compressed = zlib.compress(raw_rows)

    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", ihdr_data)
        + png_chunk(b"IDAT", compressed)
        + png_chunk(b"IEND", b"")
    )
    return png


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=BASE_URL)
    parser.add_argument("--group-id", default=GROUP_ID)
    parser.add_argument(
        "--restore",
        metavar="GROUP_ID",
        help="Restore archived cases for GROUP_ID: unarchive + re-index into SCRAG, then exit.",
    )
    args = parser.parse_args()

    if args.restore:
        base = args.url.rstrip("/")
        print(f"Restoring cases for group {args.restore[:30]}...")
        resp = _post(f"{base}/debug/reindex-group", {
            "group_id": args.restore,
            "unarchive": True,
        })
        print(f"  Unarchived: {resp.get('unarchived')}")
        print(f"  Reindexed:  {resp.get('reindexed')}")
        print(f"  Case IDs:   {resp.get('case_ids')}")
        return

    base = args.url.rstrip("/")
    group_id = args.group_id

    print(f"Target: {base}")
    print(f"Group:  {group_id}\n")

    # 1. Create debug history token
    print("Step 1: Creating debug history token...")
    try:
        tok_resp = _post(f"{base}/history/token", {
            "admin_id": ADMIN_ID,
            "group_id": group_id,
        })
    except Exception as e:
        print(f"  ERROR: {e}")
        print("  Make sure HTTP_DEBUG_ENDPOINTS_ENABLED=true on the server.")
        sys.exit(1)
    token = tok_resp["token"]
    print(f"  token={token[:16]}...")

    # 2. Build synthetic messages with one image attachment
    print("\nStep 2: Building synthetic test payload with image attachment...")
    now_ms = int(time.time() * 1000)

    def sender_hash(s: str) -> str:
        return hashlib.sha256(s.encode()).hexdigest()[:16]

    img_bytes = _make_test_png(60, 60, r=41, g=128, b=185)
    img_b64 = base64.b64encode(img_bytes).decode()

    msg_with_image_id = "test_msg_img_001"
    msg_text_id = "test_msg_txt_001"
    msg_solution_id = "test_msg_sol_001"

    messages = [
        {
            "message_id": msg_with_image_id,
            "sender_hash": sender_hash("user_alpha"),
            "sender_name": "Alpha User",
            "ts": now_ms - 3600_000,
            "content_text": "Screensharing issues – black screen on startup [image]\n{\"extracted_text\": \"Error: display init failed\", \"observations\": [\"black screen\", \"error dialog\"]}",
            "image_payloads": [
                {
                    "filename": "screenshot.png",
                    "content_type": "image/png",
                    "data_b64": img_b64,
                }
            ],
        },
        {
            "message_id": msg_solution_id,
            "sender_hash": sender_hash("admin_beta"),
            "sender_name": "Beta Admin",
            "ts": now_ms - 3000_000,
            "content_text": "Restart the display service: sudo systemctl restart display-manager. This clears the init error.",
            "image_payloads": [],
        },
        {
            "message_id": msg_text_id,
            "sender_hash": sender_hash("user_alpha"),
            "sender_name": "Alpha User",
            "ts": now_ms - 2700_000,
            "content_text": "Worked! Thanks.",
            "image_payloads": [],
        },
    ]

    case_block = f"""{sender_hash('user_alpha')} ts={now_ms - 3600_000} msg_id={msg_with_image_id}
Screensharing issues – black screen on startup [image]
{{"extracted_text": "Error: display init failed", "observations": ["black screen", "error dialog"]}}

{sender_hash('admin_beta')} ts={now_ms - 3000_000} msg_id={msg_solution_id}
Restart the display service: sudo systemctl restart display-manager. This clears the init error.

{sender_hash('user_alpha')} ts={now_ms - 2700_000} msg_id={msg_text_id}
Worked! Thanks."""

    payload = {
        "token": token,
        "group_id": group_id,
        "cases": [{"case_block": case_block}],
        "messages": messages,
    }

    # 3. Post to /history/cases
    print("Step 3: Posting to /history/cases...")
    try:
        ingest_resp = _post(f"{base}/history/cases", payload)
    except Exception as e:
        print(f"  ERROR posting cases: {e}")
        sys.exit(1)

    case_ids = ingest_resp.get("case_ids", [])
    inserted = ingest_resp.get("cases_inserted", 0)
    print(f"  Inserted: {inserted} case(s)")
    print(f"  Case IDs: {case_ids}")

    if not case_ids:
        print("\nNo cases were inserted. The LLM may have filtered out the test case.")
        sys.exit(0)

    # 4. Verify image appears on case page
    print("\nStep 4: Verifying case page and media links...")
    results = []
    for cid in case_ids:
        case_url = f"{base}/case/{cid}"
        api_url = f"{base}/api/cases/{cid}"
        try:
            api_data = json.loads(_get(api_url))
            evidence = api_data.get("evidence", [])
            all_images = []
            for ev in evidence:
                all_images.extend(ev.get("images", []))
            page_html = _get(case_url).decode("utf-8", errors="replace")
            img_in_page = any(img in page_html for img in all_images) if all_images else False
            results.append({
                "case_id": cid,
                "url": case_url,
                "title": api_data.get("problem_title", "?"),
                "status": api_data.get("status", "?"),
                "images": all_images,
                "img_on_page": img_in_page,
            })
        except Exception as e:
            results.append({
                "case_id": cid,
                "url": case_url,
                "title": "ERROR",
                "status": "?",
                "images": [],
                "img_on_page": False,
                "error": str(e),
            })

    # 5. Print table
    print("\n" + "=" * 80)
    print("RESULTS TABLE")
    print("=" * 80)
    for r in results:
        print(f"\nCase:    {r['url']}")
        print(f"Title:   {r.get('title', '?')}")
        print(f"Status:  {r.get('status', '?')}")
        if r.get("error"):
            print(f"Error:   {r['error']}")
        elif r["images"]:
            for img in r["images"]:
                print(f"Image:   {base}{img}")
            print(f"Shown:   {'✓ yes' if r['img_on_page'] else '✗ NOT found in page HTML'}")
        else:
            print("Images:  none stored for this case")
    print("=" * 80)

    # 6. List all current cases for the group
    print(f"\nStep 5: All cases for group {group_id[:20]}...")
    try:
        group_cases_url = f"{base}/api/group-cases?group_id={urllib.parse.quote(group_id)}"
        cases_data = json.loads(_get(group_cases_url))
        all_cases = cases_data.get("cases", [])
        print(f"\n{'Case ID':<36} {'Status':<10} {'Title'}")
        print("-" * 90)
        for c in all_cases:
            cid = c.get("case_id", "?")[:36]
            st = c.get("status", "?")[:10]
            ti = (c.get("problem_title") or "?")[:50]
            print(f"{cid:<36} {st:<10} {ti}")
        if not all_cases:
            print("  (no cases)")
    except Exception as e:
        print(f"  Could not list group cases: {e}")

    print("\nDone.")


if __name__ == "__main__":
    main()
