#!/usr/bin/env python3
"""
Read Signal Desktop database using decrypted key.

Prerequisites:
1. Run decrypt_key.ps1 on original Windows machine to get the key
2. Place signal_key.txt in test/data/signal_key.txt
3. Have the Signal backup extracted in test/data/extracted/Signal1/

Usage:
    python test/read_signal_db.py

This will export messages to test/data/signal_messages.json
"""

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR / "data"
KEY_PATH = DATA_DIR / "signal_key.txt"
DB_PATH = DATA_DIR / "extracted" / "Signal1" / "sql" / "db.sqlite"
OUTPUT_PATH = DATA_DIR / "signal_messages.json"


def main():
    print("Signal Database Reader")
    print("=" * 50)
    
    # Check key file
    if not KEY_PATH.exists():
        print(f"ERROR: Key file not found: {KEY_PATH}")
        print("\nTo get the key:")
        print("1. On the original Windows machine, run: decrypt_key.ps1")
        print("2. Copy Desktop\\signal_key.txt to test/data/signal_key.txt")
        sys.exit(1)
    
    # Check database
    if not DB_PATH.exists():
        print(f"ERROR: Database not found: {DB_PATH}")
        print("\nExtract the Signal backup first:")
        print("  cd test/data && unzip Signal1-*.zip -d extracted/")
        sys.exit(1)
    
    # Read key
    key_hex = KEY_PATH.read_text().strip()
    print(f"Key: {key_hex[:16]}... ({len(key_hex)} chars)")
    
    if len(key_hex) != 64:
        print(f"WARNING: Expected 64 char key (32 bytes), got {len(key_hex)}")
    
    # Try to import sqlcipher
    try:
        from pysqlcipher3 import dbapi2 as sqlcipher
        print("Using pysqlcipher3")
    except ImportError:
        print("\nERROR: pysqlcipher3 not installed")
        print("Install with: pip install pysqlcipher3")
        print("\nOn Ubuntu, you may need: sudo apt-get install libsqlcipher-dev")
        sys.exit(1)
    
    # Connect to database
    print(f"\nOpening database: {DB_PATH}")
    conn = sqlcipher.connect(str(DB_PATH))
    
    # Signal Desktop uses SQLCipher 4 settings
    try:
        conn.execute(f"PRAGMA key=\"x'{key_hex}'\"")
        conn.execute("PRAGMA cipher_page_size = 4096")
        conn.execute("PRAGMA kdf_iter = 64000")
        conn.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA512")
        conn.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512")
        
        # Test query
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        print(f"SUCCESS! Found {len(tables)} tables")
        
    except Exception as e:
        print(f"\nERROR: Failed to open database: {e}")
        print("\nPossible causes:")
        print("  - Wrong key (from different Signal installation)")
        print("  - Database corrupted")
        print("  - SQLCipher version mismatch")
        sys.exit(1)
    
    # List tables
    print("\nTables:")
    for (name,) in tables:
        print(f"  - {name}")
    
    # Get conversations
    print("\n" + "=" * 50)
    print("CONVERSATIONS")
    print("=" * 50)
    
    conversations = {}
    try:
        for row in conn.execute(
            "SELECT id, name, type, profileName FROM conversations"
        ):
            cid, name, ctype, profile = row
            display_name = name or profile or f"(unnamed {ctype})"
            conversations[cid] = {
                "name": display_name,
                "type": ctype,
            }
            print(f"  [{ctype}] {display_name}")
    except Exception as e:
        print(f"  Error reading conversations: {e}")
    
    # Find tech support group
    print("\n" + "=" * 50)
    print("SEARCHING FOR TECH SUPPORT GROUP")
    print("=" * 50)
    
    target_keywords = ["техпідтримка", "підтримка", "support", "академія", "стабх"]
    target_group = None
    
    for cid, info in conversations.items():
        name_lower = (info.get("name") or "").lower()
        if any(kw in name_lower for kw in target_keywords):
            target_group = cid
            print(f"  Found: {info['name']}")
            break
    
    if not target_group:
        print("  No tech support group found by name")
        print("  Will export messages from ALL group conversations")
    
    # Export messages
    print("\n" + "=" * 50)
    print("EXPORTING MESSAGES")
    print("=" * 50)
    
    messages = []
    query = """
        SELECT 
            m.id,
            m.conversationId,
            m.sent_at,
            m.body,
            m.type,
            m.source,
            m.sourceUuid
        FROM messages m
        WHERE m.body IS NOT NULL AND m.body != ''
        ORDER BY m.sent_at
    """
    
    try:
        for row in conn.execute(query):
            msg_id, conv_id, sent_at, body, msg_type, source, source_uuid = row
            
            # Filter to target group if found
            if target_group and conv_id != target_group:
                continue
            
            # Get conversation info
            conv_info = conversations.get(conv_id, {})
            
            messages.append({
                "id": msg_id,
                "conversation_id": conv_id,
                "conversation_name": conv_info.get("name", ""),
                "timestamp": sent_at,
                "body": body,
                "type": msg_type,
                "sender": source_uuid or source or "",
            })
    except Exception as e:
        print(f"  Error reading messages: {e}")
    
    print(f"  Exported {len(messages)} messages")
    
    # Save to JSON
    output = {
        "conversations": conversations,
        "messages": messages,
        "target_group": target_group,
    }
    
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    
    print(f"\nSaved to: {OUTPUT_PATH}")
    
    # Show sample messages
    if messages:
        print("\n" + "=" * 50)
        print("SAMPLE MESSAGES (first 10)")
        print("=" * 50)
        for msg in messages[:10]:
            text = msg["body"][:80] + "..." if len(msg["body"]) > 80 else msg["body"]
            print(f"  [{msg['conversation_name'][:20]}] {text}")
    
    conn.close()
    print("\nDone!")


if __name__ == "__main__":
    main()
