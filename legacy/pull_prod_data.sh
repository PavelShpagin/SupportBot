#!/bin/bash
# Pull real messages + images from prod VM for local ingestion testing
set -e

SSH="ssh -i ~/.ssh/supportbot_ed25519 -o StrictHostKeyChecking=no opc@161.33.64.115"
GROUP_ID="1fWBz1RwCF0B/wGHfNMER4NkWBJPYvjGCv2kXsBJTok="
OUT_DIR="/home/pavel/dev/SupportBot/tests/fixtures"

echo "=== Pulling raw messages from prod DB ==="
$SSH "docker exec supportbot-db-1 mysql -u supportbot -psupportbot supportbot \
  -e \"SELECT message_id, group_id, ts, sender_hash, sender_name, content_text, image_paths_json, reply_to_id \
      FROM raw_messages WHERE group_id='$GROUP_ID' ORDER BY ts ASC\" 2>/dev/null" \
  > /tmp/prod_messages_raw.tsv

echo "Rows pulled: $(wc -l < /tmp/prod_messages_raw.tsv)"

echo "=== Converting to JSON ==="
python3 << 'PYEOF'
import json, csv, sys

rows = []
with open('/tmp/prod_messages_raw.tsv', 'r', encoding='utf-8') as f:
    reader = csv.DictReader(f, delimiter='\t')
    for row in reader:
        rows.append({
            "id": row["message_id"],
            "ts": int(row["ts"]) if row["ts"] else 0,
            "sender": row["sender_hash"],
            "sender_name": row["sender_name"] if row["sender_name"] != "NULL" else None,
            "body": row["content_text"] if row["content_text"] != "NULL" else "",
            "image_paths": json.loads(row["image_paths_json"]) if row["image_paths_json"] and row["image_paths_json"] != "NULL" else [],
            "reply_to_id": row["reply_to_id"] if row["reply_to_id"] != "NULL" else None,
            "reactions": 0,
        })

print(f"Parsed {len(rows)} messages, {sum(1 for r in rows if r['image_paths'])} with images")
with open('/home/pavel/dev/SupportBot/tests/fixtures/prod_chat.json', 'w', encoding='utf-8') as f:
    json.dump(rows, f, ensure_ascii=False, indent=2)
print("Written to tests/fixtures/prod_chat.json")
PYEOF

echo "=== Pulling images from VM ==="
$SSH "ls /var/lib/signal/bot/history/$GROUP_ID/ 2>/dev/null | head -20" || true

# Create local image dir
mkdir -p "$OUT_DIR/images"

# Pull each image
$SSH "ls /var/lib/signal/bot/history/$GROUP_ID/*.png 2>/dev/null" | while read remote_path; do
    fname=$(basename "$remote_path")
    echo "  Pulling $fname..."
    scp -i ~/.ssh/supportbot_ed25519 -o StrictHostKeyChecking=no \
        "opc@161.33.64.115:$remote_path" "$OUT_DIR/images/$fname" 2>/dev/null || echo "  Failed: $fname"
done

echo "=== Done ==="
ls -la "$OUT_DIR/images/" 2>/dev/null || echo "No images dir"
