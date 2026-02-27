#!/usr/bin/env python3
"""
Pull real messages + images from prod VM and save as fixture for local ingestion.

Pulls from ALL groups (real group + group-x) so we get ~180 messages total.
Images are pulled from disk (both DB-tracked and disk-only).

Usage: python3 legacy/pull_prod_data.py
"""
import base64
import json
import os
import subprocess
from pathlib import Path

SSH_KEY = os.path.expanduser("~/.ssh/supportbot_ed25519")
VM = "opc@161.33.64.115"
PROD_URL = "https://supportbot.info"

# Pull from group-x (has ~60 msgs + many images on disk) and the real group (~120 msgs + 4 images)
GROUPS = [
    "group-x",
    "1fWBz1RwCF0B/wGHfNMER4NkWBJPYvjGCv2kXsBJTok=",
]

OUT_DIR = Path(__file__).parent.parent / "tests" / "fixtures"
IMG_DIR = OUT_DIR / "images"
OUT_JSON = OUT_DIR / "prod_chat.json"


def ssh(cmd):
    result = subprocess.run(
        ["ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no", VM, cmd],
        capture_output=True,
    )
    return result.stdout, result.stderr


def scp(remote_path, local_path):
    r = subprocess.run(
        ["scp", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
         f"{VM}:{remote_path}", str(local_path)],
        capture_output=True,
    )
    return r.returncode == 0


all_messages = []

for group_id in GROUPS:
    print(f"\n=== Pulling messages for group: {group_id[:30]}... ===")
    sql = (
        "SELECT message_id, ts, sender_hash, sender_name, content_text, "
        "image_paths_json, reply_to_id "
        f"FROM raw_messages WHERE group_id='{group_id}' ORDER BY ts ASC"
    )
    stdout, _ = ssh(
        f"docker exec supportbot-db-1 mysql -u supportbot -psupportbot supportbot "
        f"--batch --skip-column-names -e \"{sql}\" 2>/dev/null"
    )
    lines = stdout.decode("utf-8", errors="replace").strip().split("\n")
    print(f"  Got {len(lines)} rows")

    for line in lines:
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        msg_id, ts, sender_hash, sender_name, content_text, image_paths_json, reply_to_id = parts[:7]
        try:
            img_paths = json.loads(image_paths_json) if image_paths_json and image_paths_json != "NULL" else []
        except Exception:
            img_paths = []
        all_messages.append({
            "id": msg_id,
            "ts": int(ts) if ts.isdigit() else 0,
            "sender": sender_hash,
            "sender_name": None if sender_name == "NULL" else sender_name,
            "body": "" if content_text == "NULL" else content_text,
            "image_paths": img_paths,
            "_group_id": group_id,
            "reply_to_id": None if reply_to_id == "NULL" else reply_to_id,
            "reactions": 0,
        })

# Sort all messages by ts
all_messages.sort(key=lambda m: m["ts"])
print(f"\nTotal messages across all groups: {len(all_messages)}")

# Pull images: from DB-tracked paths + scan disk for group-x
IMG_DIR.mkdir(parents=True, exist_ok=True)
print("\n=== Pulling images ===")

# Collect all remote paths: DB-tracked
remote_paths = {}
for msg in all_messages:
    for p in msg.get("image_paths", []):
        fname = os.path.basename(p)
        remote_paths[fname] = p

# Also scan disk for group-x (images not tracked in DB)
for group_id in GROUPS:
    stdout, _ = ssh(f"ls /var/lib/signal/bot/history/{group_id}/ 2>/dev/null")
    files = stdout.decode().strip().split("\n")
    for fname in files:
        fname = fname.strip()
        if fname and fname not in remote_paths:
            remote_paths[fname] = f"/var/lib/signal/bot/history/{group_id}/{fname}"

print(f"  Found {len(remote_paths)} unique image files to pull")
for fname, remote_path in remote_paths.items():
    local_path = IMG_DIR / fname
    print(f"  {fname}...", end=" ", flush=True)
    ok = scp(remote_path, local_path)
    if ok and local_path.exists():
        print(f"OK ({local_path.stat().st_size} bytes)")
    else:
        print("FAILED")

# Build a map: msg_id -> image files (from disk scan for group-x)
# For group-x messages, match images by msg_id prefix in filename
print("\n=== Matching disk images to group-x messages ===")
disk_images = {f.name: f for f in IMG_DIR.iterdir() if f.is_file()}

for msg in all_messages:
    if msg.get("image_paths"):
        continue  # already has DB-tracked paths
    msg_id = msg["id"]
    # Match filenames like local-img-XXXXXXXX_0.png to msg_id
    matched = [fname for fname in disk_images if msg_id in fname or fname.startswith(msg_id)]
    if matched:
        msg["image_paths"] = [
            f"/var/lib/signal/bot/history/{msg['_group_id']}/{fname}"
            for fname in matched
        ]
        print(f"  {msg_id}: matched {matched}")

# Embed images as base64 + build image_url for display
print("\n=== Embedding images as base64 ===")
for msg in all_messages:
    if not msg.get("image_paths"):
        continue
    payloads = []
    urls = []
    group_id = msg["_group_id"]
    for remote_path in msg["image_paths"]:
        fname = os.path.basename(remote_path)
        local_path = IMG_DIR / fname
        if local_path.exists():
            data = local_path.read_bytes()
            payloads.append({
                "filename": fname,
                "content_type": "image/png",
                "data_b64": base64.b64encode(data).decode(),
            })
            # Build the public URL: /static/bot/history/<group_id>/<fname>
            url = f"{PROD_URL}/static/bot/history/{group_id}/{fname}"
            urls.append(url)
            print(f"  {fname}: {len(data)} bytes  →  {url}")
    if payloads:
        msg["_image_payloads"] = payloads
        msg["_image_urls"] = urls

# Remove internal _group_id field before saving
for msg in all_messages:
    msg.pop("_group_id", None)

# Save fixture
with open(OUT_JSON, "w", encoding="utf-8") as f:
    json.dump(all_messages, f, ensure_ascii=False, indent=2)

with_images = [m for m in all_messages if m.get("_image_payloads")]
print(f"\n=== Saved {len(all_messages)} messages to {OUT_JSON} ===")
print(f"  With embedded images: {len(with_images)}")
print(f"\n=== Image URLs (verify these are live) ===")
for msg in with_images:
    for url in msg.get("_image_urls", []):
        print(f"  {url}")
