"""
One-shot script: delete every ChromaDB entry whose case_id no longer exists in MySQL.
Run inside the signal-bot container:
  docker compose exec signal-bot python3 /app/fix_chroma_orphans.py
"""
import sys
sys.path.insert(0, "/app")

from app.config import load_settings
from app.db import create_db, get_case
from app.rag.chroma import create_chroma

settings = load_settings()
db = create_db(settings)
rag = create_chroma(settings)

col = rag._collection()
all_docs = col.get(include=["metadatas"])
all_ids = all_docs.get("ids", [])
metadatas = all_docs.get("metadatas", [])

print(f"ChromaDB total entries: {len(all_ids)}")

orphans = []
for i, cid in enumerate(all_ids):
    if get_case(db, cid) is None:
        group = (metadatas[i] or {}).get("group_id", "?")[:20] if i < len(metadatas) else "?"
        orphans.append(cid)
        print(f"  ORPHAN: {cid}  (group={group})")

print(f"\nOrphaned entries (in Chroma, missing from MySQL): {len(orphans)}")

if orphans:
    col.delete(ids=orphans)
    print(f"Deleted {len(orphans)} orphaned entries from ChromaDB.")
else:
    print("ChromaDB is clean — no orphans.")
