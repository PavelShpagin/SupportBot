#!/usr/bin/env python3
"""
Pull real messages + images from prod VM and save as fixture for local ingestion.
Usage: python3 legacy/pull_prod_data.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

SSH_KEY = os.path.expanduser("~/.ssh/supportbot_ed25519")
VM = "opc@161.33.64.115"
GROUP_ID = "1fWBz1RwCF0B/wGHfNMER4NkWBJPYvjGCv2kXsBJTok="
OUT_DIR = Path(__file__).parent.parent / "tests" / "fixtures"
IMG_DIR = OUT_DIR / "images"
OUT_JSON = OUT_DIR / "prod_chat.json"

def ssh(cmd):
    result = subprocess.run(
        ["ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no", VM, cmd],
        capture_output=True
    )
    return result.stdout, result.stderr

def scp(remote_path, local_path):
    subprocess.run(
        ["scp", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
         f"{VM}:{remote_path}", str(local_path)],
        capture_output=True
    )

print("=== Pulling messages from prod DB ===")
sql = (
    "SELECT message_id, ts, sender_hash, sender_name, content_text, "
    "image_paths_json, reply_to_id "
    f"FROM raw_messages WHERE group_id='{GROUP_ID}' ORDER BY ts ASC"
)
stdout, stderr = ssh(
    f"docker exec supportbot-db-1 mysql -u supportbot -psupportbot supportbot "
    f"--batch --skip-column-names -e \"{sql}\" 2>/dev/null"
)

lines = stdout.decode("utf-8", errors="replace").strip().split("\n")
print(f"  Got {len(lines)} rows")

messages = []
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
    messages.append({
        "id": msg_id,
        "ts": int(ts) if ts.isdigit() else 0,
        "sender": sender_hash,
        "sender_name": None if sender_name == "NULL" else sender_name,
        "body": "" if content_text == "NULL" else content_text,
        "image_paths": img_paths,
        "reply_to_id": None if reply_to_id == "NULL" else reply_to_id,
        "reactions": 0,
    })

with_images = [m for m in messages if m["image_paths"]]
print(f"  {len(messages)} messages, {len(with_images)} with images")

# Pull images
IMG_DIR.mkdir(parents=True, exist_ok=True)
print(f"\n=== Pulling {len(with_images)} images ===")
for msg in with_images:
    for remote_path in msg["image_paths"]:
        fname = os.path.basename(remote_path)
        local_path = IMG_DIR / fname
        print(f"  {fname}...", end=" ", flush=True)
        scp(remote_path, local_path)
        if local_path.exists():
            print(f"OK ({local_path.stat().st_size} bytes)")
        else:
            print("FAILED")

# Embed images as base64 in messages
import base64
print("\n=== Embedding images as base64 ===")
for msg in messages:
    if not msg["image_paths"]:
        continue
    payloads = []
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
            print(f"  {fname}: {len(data)} bytes embedded")
    if payloads:
        msg["_image_payloads"] = payloads

# Save fixture
OUT_DIR.mkdir(parents=True, exist_ok=True)
with open(OUT_JSON, "w", encoding="utf-8") as f:
    json.dump(messages, f, ensure_ascii=False, indent=2)

print(f"\n=== Saved {len(messages)} messages to {OUT_JSON} ===")
print(f"  With embedded images: {sum(1 for m in messages if m.get('_image_payloads'))}")
