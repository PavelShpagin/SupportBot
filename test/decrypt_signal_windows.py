#!/usr/bin/env python3
"""
Decrypt Signal Desktop database on Windows.

This script must be run on Windows (not WSL) because it uses DPAPI
which is tied to the Windows user account.

Usage (from Windows PowerShell):
    python decrypt_signal_windows.py
"""

import json
import os
import sys
from pathlib import Path

# Check we're on Windows
if sys.platform != "win32":
    print("ERROR: This script must be run on Windows, not WSL/Linux")
    print("Run from Windows PowerShell: python decrypt_signal_windows.py")
    sys.exit(1)

try:
    import win32crypt
except ImportError:
    print("Installing pywin32...")
    os.system("pip install pywin32")
    import win32crypt

# Paths
SCRIPT_DIR = Path(__file__).parent
EXTRACTED_DIR = SCRIPT_DIR / "data" / "extracted" / "Signal1"
CONFIG_PATH = EXTRACTED_DIR / "config.json"
DB_PATH = EXTRACTED_DIR / "sql" / "db.sqlite"
OUTPUT_PATH = SCRIPT_DIR / "data" / "decrypted_messages.json"


def decrypt_key(encrypted_key_hex: str) -> bytes:
    """Decrypt the Signal key using Windows DPAPI."""
    encrypted_key = bytes.fromhex(encrypted_key_hex)
    decrypted = win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)
    return decrypted[1]


def main():
    print("Signal Desktop Database Decryption")
    print("=" * 50)
    
    if not CONFIG_PATH.exists():
        print(f"ERROR: config.json not found at {CONFIG_PATH}")
        print("First extract the zip file:")
        print("  cd test/data")
        print("  unzip Signal1-*.zip -d extracted/")
        sys.exit(1)
    
    if not DB_PATH.exists():
        print(f"ERROR: db.sqlite not found at {DB_PATH}")
        sys.exit(1)
    
    # Load encrypted key
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    
    encrypted_key_hex = config.get("encryptedKey")
    if not encrypted_key_hex:
        print("ERROR: encryptedKey not found in config.json")
        sys.exit(1)
    
    print(f"Encrypted key: {encrypted_key_hex[:20]}...")
    
    # Decrypt key using DPAPI
    try:
        key = decrypt_key(encrypted_key_hex)
        print(f"Decrypted key length: {len(key)} bytes")
        print(f"Key (hex): {key.hex()[:20]}...")
    except Exception as e:
        print(f"ERROR: Failed to decrypt key: {e}")
        print("\nThis error means one of:")
        print("  1. The database was encrypted on a different Windows user account")
        print("  2. The database was encrypted on a different computer")
        print("  3. Windows credentials have changed since encryption")
        sys.exit(1)
    
    # Try to open with sqlcipher
    try:
        import pysqlcipher3.dbapi2 as sqlcipher
    except ImportError:
        print("\nInstalling pysqlcipher3...")
        os.system("pip install pysqlcipher3")
        import pysqlcipher3.dbapi2 as sqlcipher
    
    print("\nConnecting to database...")
    conn = sqlcipher.connect(str(DB_PATH))
    
    # Try different SQLCipher configurations
    configs = [
        # Signal Desktop default (SQLCipher 4.x)
        {
            "cipher_page_size": 4096,
            "kdf_iter": 64000,
            "cipher_hmac_algorithm": "HMAC_SHA512",
            "cipher_kdf_algorithm": "PBKDF2_HMAC_SHA512",
        },
        # Alternative config
        {
            "cipher_page_size": 1024,
            "kdf_iter": 64000,
            "cipher_hmac_algorithm": "HMAC_SHA1",
            "cipher_kdf_algorithm": "PBKDF2_HMAC_SHA1",
        },
    ]
    
    for cfg in configs:
        try:
            conn.execute(f"PRAGMA key=\"x'{key.hex()}'\"")
            conn.execute(f"PRAGMA cipher_page_size = {cfg['cipher_page_size']}")
            conn.execute(f"PRAGMA kdf_iter = {cfg['kdf_iter']}")
            conn.execute(f"PRAGMA cipher_hmac_algorithm = {cfg['cipher_hmac_algorithm']}")
            conn.execute(f"PRAGMA cipher_kdf_algorithm = {cfg['cipher_kdf_algorithm']}")
            
            # Test query
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            print(f"\nSuccess! Found {len(tables)} tables")
            break
        except Exception as e:
            print(f"Config {cfg} failed: {e}")
            continue
    else:
        print("ERROR: Could not open database with any configuration")
        sys.exit(1)
    
    # Export messages
    print("\nExporting messages...")
    
    # Get conversations
    conversations = {}
    for row in conn.execute("SELECT id, name, type FROM conversations"):
        conversations[row[0]] = {"name": row[1], "type": row[2]}
    
    print(f"Found {len(conversations)} conversations")
    
    # Find target group
    target_group = None
    for cid, info in conversations.items():
        name = info.get("name") or ""
        if "Техпідтримка" in name or "СтабХ" in name or "Академія" in name:
            target_group = cid
            print(f"Found target group: {name}")
            break
    
    if not target_group:
        print("WARNING: Target group not found, exporting all group messages")
    
    # Export messages
    messages = []
    query = """
        SELECT m.id, m.conversationId, m.sent_at, m.body, m.type,
               c.name as group_name
        FROM messages m
        LEFT JOIN conversations c ON m.conversationId = c.id
        WHERE m.body IS NOT NULL AND m.body != ''
        ORDER BY m.sent_at
    """
    
    for row in conn.execute(query):
        msg = {
            "id": row[0],
            "conversation_id": row[1],
            "timestamp": row[2],
            "body": row[3],
            "type": row[4],
            "group_name": row[5],
        }
        
        if target_group and row[1] != target_group:
            continue
            
        messages.append(msg)
    
    print(f"Exported {len(messages)} messages")
    
    # Save to JSON
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "conversations": conversations,
            "messages": messages,
            "target_group": target_group,
        }, f, ensure_ascii=False, indent=2)
    
    print(f"\nSaved to: {OUTPUT_PATH}")
    conn.close()


if __name__ == "__main__":
    main()
