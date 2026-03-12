#!/usr/bin/env python3
"""
Re-export SupportBench datasets WITH media downloads.

For each of the 6 SupportBench groups, re-fetches the same 10K messages
and downloads all media (photos, videos, documents, audio) to local disk.

Media files are saved as: datasets/{name}/media/{msg_id}_{filename}
The dataset JSON is updated with local media paths.

Usage:
    python scripts/export_telegram_media.py              # all 6 datasets
    python scripts/export_telegram_media.py lineageos     # single dataset
"""
import asyncio
import json
import hashlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter

from telethon import TelegramClient
from telethon.tl.types import (
    MessageMediaPhoto, MessageMediaDocument,
    MessageMediaWebPage, MessageMediaContact,
    MessageMediaGeo, MessageMediaPoll,
)

# ── Config ──────────────────────────────────────────────────────────
API_ID = int(os.environ.get("TELEGRAM_API_ID", 37616170))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "9088e884b78d5add887e1d3fc5bb88cb")
SESSION_FILE = str(Path(__file__).parent.parent / "local_data" / "telegram_session")

DATASETS_DIR = Path(__file__).parent.parent / "datasets"

# SupportBench final 6 datasets
GROUPS = {
    "ua_ardupilot":  "https://t.me/ardupilot_ua",
    "ua_selfhosted": "https://t.me/selfhostedua",
    "domotica_es":   "https://t.me/GizChinaHomeAssistant",
    "naseros":       "https://t.me/NASeros",
    "lineageos":     "https://t.me/Lineageos_group",
    "tasmota":       "https://t.me/tasmota",
}

TARGET = 10000  # exact number of non-empty messages per group

# Max file size to download (skip huge videos)
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # 20 MB
# Media types worth downloading (skip audio, skip videos > threshold)
DOWNLOAD_TYPES = {"photo", "image", "document", "pdf", "video", "archive"}


# ── Helpers ─────────────────────────────────────────────────────────
def anonymize_id(sender_id: int) -> str:
    if sender_id is None:
        return "unknown"
    return "user_" + hashlib.sha256(str(sender_id).encode()).hexdigest()[:10]


def classify_media(msg) -> str | None:
    if isinstance(msg.media, MessageMediaPhoto):
        return "photo"
    if isinstance(msg.media, MessageMediaDocument):
        doc = msg.media.document
        if doc:
            mime = doc.mime_type or ""
            if "video" in mime:
                return "video"
            if "image" in mime:
                return "image"
            if "audio" in mime or "ogg" in mime:
                return "audio"
            if "pdf" in mime:
                return "pdf"
            if "zip" in mime or "rar" in mime or "tar" in mime or "gzip" in mime:
                return "archive"
            for attr in doc.attributes:
                if hasattr(attr, "file_name"):
                    ext = attr.file_name.rsplit(".", 1)[-1].lower()
                    if ext in ("mp4", "webm", "avi", "mov", "mkv"):
                        return "video"
                    if ext in ("jpg", "jpeg", "png", "gif", "webp", "bmp"):
                        return "image"
                    if ext in ("mp3", "ogg", "wav", "flac", "m4a"):
                        return "audio"
                    if ext == "pdf":
                        return "pdf"
                    if ext in ("zip", "rar", "tar", "gz", "7z"):
                        return "archive"
        return "document"
    if isinstance(msg.media, MessageMediaWebPage):
        return "webpage"
    if isinstance(msg.media, MessageMediaContact):
        return "contact"
    if isinstance(msg.media, MessageMediaGeo):
        return "geo"
    if isinstance(msg.media, MessageMediaPoll):
        return "poll"
    if msg.media is not None:
        return "other"
    return None


def media_extension(msg) -> str:
    """Best-effort file extension for the media."""
    if isinstance(msg.media, MessageMediaPhoto):
        return ".jpg"
    if isinstance(msg.media, MessageMediaDocument):
        doc = msg.media.document
        if doc:
            # Try to get from file_name attribute
            for attr in doc.attributes:
                if hasattr(attr, "file_name") and attr.file_name:
                    parts = attr.file_name.rsplit(".", 1)
                    if len(parts) == 2:
                        return "." + parts[1].lower()
            # Fallback to mime type
            mime = doc.mime_type or ""
            mime_ext = {
                "video/mp4": ".mp4",
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/webp": ".webp",
                "image/gif": ".gif",
                "audio/ogg": ".ogg",
                "audio/mpeg": ".mp3",
                "application/pdf": ".pdf",
                "application/zip": ".zip",
                "application/x-rar": ".rar",
            }
            for m, ext in mime_ext.items():
                if m in mime:
                    return ext
    return ".bin"


async def export_group(client: TelegramClient, name: str, link: str):
    """Export messages + media for a single group."""
    media_dir = DATASETS_DIR / name / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"Exporting: {name} ({link})")
    print(f"Media dir: {media_dir}")
    print(f"{'='*60}")

    try:
        entity = await client.get_entity(link)
    except Exception as e:
        print(f"  ERROR: Could not access {link}: {e}")
        return

    messages = []
    media_stats = Counter()
    download_errors = 0
    skipped_large = 0
    count = 0

    skipped_empty = 0
    async for msg in client.iter_messages(entity, limit=None):
        count += 1
        if count % 500 == 0:
            print(f"  ... {count} scanned, {len(messages)} kept ({media_stats.total()} media)")

        # Skip empty/service messages — must have text OR media
        text = msg.text or ""
        media_type = classify_media(msg)
        has_reply = msg.reply_to_msg_id if msg.reply_to else None
        if not text.strip() and not media_type:
            skipped_empty += 1
            continue

        media_path = None

        # Download media if present and worth downloading
        if media_type and media_type in DOWNLOAD_TYPES:
            # Check file size before downloading
            file_size = 0
            if isinstance(msg.media, MessageMediaDocument) and msg.media.document:
                file_size = msg.media.document.size or 0
            if file_size > MAX_DOWNLOAD_BYTES:
                skipped_large += 1
                # Still record media_type but no local path
            else:
                ext = media_extension(msg)
                filename = f"{msg.id}{ext}"
                filepath = media_dir / filename
                try:
                    if not filepath.exists():  # skip if already downloaded
                        await client.download_media(msg, file=str(filepath))
                    if filepath.exists():
                        media_path = f"media/{filename}"
                        media_stats[media_type] += 1
                except Exception as e:
                    download_errors += 1
                    if download_errors <= 5:
                        print(f"  WARN: Failed to download media for msg {msg.id}: {e}")

        # Extract web page URL if present
        webpage_url = None
        if isinstance(msg.media, MessageMediaWebPage) and msg.media.webpage:
            wp = msg.media.webpage
            if hasattr(wp, "url"):
                webpage_url = wp.url

        messages.append({
            "id": msg.id,
            "date": msg.date.isoformat() if msg.date else None,
            "sender": anonymize_id(msg.sender_id),
            "text": text,
            "reply_to": has_reply,
            "grouped_id": msg.grouped_id,  # album grouping (multi-photo posts)
            "media_type": media_type,
            "media_path": media_path,
            "webpage_url": webpage_url,
            "views": msg.views,
            "forwards": msg.forwards,
            "reactions": {
                getattr(r.reaction, "emoticon", "custom"): r.count
                for r in (msg.reactions.results if msg.reactions else [])
            },
        })

        # Stop once we have exactly TARGET non-empty messages
        if len(messages) >= TARGET:
            break

    print(f"  Scanned {count}, skipped {skipped_empty} empty, kept {len(messages)}")

    # Reverse to chronological order
    messages.reverse()

    # Save JSON
    out_file = DATASETS_DIR / name / f"{name}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(messages, f, indent=2, ensure_ascii=False)

    # Stats
    total = len(messages)
    with_text = sum(1 for m in messages if m["text"].strip())
    with_reply = sum(1 for m in messages if m["reply_to"])
    with_media_file = sum(1 for m in messages if m["media_path"])
    unique_users = len(set(m["sender"] for m in messages))

    if messages:
        first_date = messages[0]["date"][:10]
        last_date = messages[-1]["date"][:10]
    else:
        first_date = last_date = "N/A"

    # Media size
    total_media_bytes = sum(
        f.stat().st_size for f in media_dir.iterdir() if f.is_file()
    )
    media_mb = total_media_bytes / (1024 * 1024)

    print(f"\n  Results for {name}:")
    print(f"  Total messages:    {total:,}")
    print(f"  With text:         {with_text:,}")
    print(f"  With replies:      {with_reply:,} ({100*with_reply/max(total,1):.1f}%)")
    print(f"  Media downloaded:  {with_media_file:,}")
    print(f"  Skipped (>20MB):   {skipped_large:,}")
    print(f"  Download errors:   {download_errors:,}")
    print(f"  Unique users:      {unique_users:,}")
    print(f"  Date range:        {first_date} → {last_date}")
    print(f"  Media size:        {media_mb:.1f} MB")
    print(f"  Media breakdown:   {dict(media_stats)}")
    print(f"  Saved to:          {out_file}")

    return {
        "name": name,
        "messages": total,
        "media_files": with_media_file,
        "media_mb": round(media_mb, 1),
        "media_breakdown": dict(media_stats),
    }


async def main():
    # Which datasets to export
    if len(sys.argv) > 1:
        targets = {k: v for k, v in GROUPS.items() if k in sys.argv[1:]}
        if not targets:
            print(f"Unknown dataset(s): {sys.argv[1:]}. Available: {list(GROUPS.keys())}")
            sys.exit(1)
    else:
        targets = GROUPS

    print("SupportBench Media Exporter")
    print(f"Session: {SESSION_FILE}")
    print(f"Output:  {DATASETS_DIR}")
    print(f"Targets: {list(targets.keys())}")
    print(f"Target:  {TARGET} non-empty messages per group")

    client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
    await client.start()
    print(f"\nLogged in as: {(await client.get_me()).first_name}")

    results = []
    for name, link in targets.items():
        result = await export_group(client, name, link)
        if result:
            results.append(result)

    await client.disconnect()

    # Summary
    print(f"\n{'='*60}")
    print(f"  EXPORT SUMMARY")
    print(f"{'='*60}")
    total_msgs = sum(r["messages"] for r in results)
    total_media = sum(r["media_files"] for r in results)
    total_mb = sum(r["media_mb"] for r in results)
    print(f"  Datasets:     {len(results)}")
    print(f"  Messages:     {total_msgs:,}")
    print(f"  Media files:  {total_media:,}")
    print(f"  Total size:   {total_mb:.1f} MB")
    for r in results:
        print(f"    {r['name']}: {r['media_files']} files, {r['media_mb']} MB — {r['media_breakdown']}")


if __name__ == "__main__":
    asyncio.run(main())
