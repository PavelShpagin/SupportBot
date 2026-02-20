#!/usr/bin/env python3
"""
Try multiple approaches to decrypt Signal Desktop database.

This script attempts various SQLCipher configurations and key formats
to open an encrypted Signal database.

Usage:
    python try_decrypt_db.py [path_to_db.sqlite] [path_to_config.json]
"""

import json
import sys
from pathlib import Path

# Default paths
SCRIPT_DIR = Path(__file__).parent
DEFAULT_DB = SCRIPT_DIR / "data" / "extracted" / "Signal1" / "sql" / "db.sqlite"
DEFAULT_CONFIG = SCRIPT_DIR / "data" / "extracted" / "Signal1" / "config.json"


def try_open_db(db_path: str, key_hex: str, config: dict) -> bool:
    """Try to open database with given configuration."""
    try:
        from pysqlcipher3 import dbapi2 as sqlcipher
    except ImportError:
        print("ERROR: pysqlcipher3 not installed")
        print("Run: pip install pysqlcipher3")
        print("On Ubuntu: sudo apt-get install libsqlcipher-dev")
        return False
    
    conn = sqlcipher.connect(db_path)
    
    try:
        # Set the key
        if config.get("key_format") == "raw":
            conn.execute(f"PRAGMA key=\"x'{key_hex}'\"")
        else:
            conn.execute(f"PRAGMA key=\"{key_hex}\"")
        
        # Set cipher parameters
        if "cipher_page_size" in config:
            conn.execute(f"PRAGMA cipher_page_size = {config['cipher_page_size']}")
        if "kdf_iter" in config:
            conn.execute(f"PRAGMA kdf_iter = {config['kdf_iter']}")
        if "cipher_hmac_algorithm" in config:
            conn.execute(f"PRAGMA cipher_hmac_algorithm = {config['cipher_hmac_algorithm']}")
        if "cipher_kdf_algorithm" in config:
            conn.execute(f"PRAGMA cipher_kdf_algorithm = {config['cipher_kdf_algorithm']}")
        if "cipher_compatibility" in config:
            conn.execute(f"PRAGMA cipher_compatibility = {config['cipher_compatibility']}")
        
        # Test query
        result = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        conn.close()
        return True
        
    except Exception as e:
        conn.close()
        return False


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else str(DEFAULT_DB)
    config_path = sys.argv[2] if len(sys.argv) > 2 else str(DEFAULT_CONFIG)
    
    print("Signal Database Decryption Attempts")
    print("=" * 60)
    
    # Check files exist
    if not Path(db_path).exists():
        print(f"ERROR: Database not found: {db_path}")
        sys.exit(1)
    
    if not Path(config_path).exists():
        print(f"ERROR: Config not found: {config_path}")
        sys.exit(1)
    
    print(f"Database: {db_path}")
    print(f"Config: {config_path}")
    
    # Read encrypted key from config
    with open(config_path) as f:
        config = json.load(f)
    
    encrypted_key = config.get("encryptedKey", "")
    print(f"\nEncrypted key from config: {encrypted_key[:40]}...")
    print(f"Key length: {len(encrypted_key)} chars ({len(encrypted_key)//2} bytes)")
    
    # The encryptedKey is DPAPI-encrypted, so we can't use it directly.
    # But let's try some common approaches anyway.
    
    # Try 1: Maybe the key IS the raw key (unlikely but let's try)
    print("\n" + "-" * 60)
    print("Attempt 1: Use encryptedKey as raw SQLCipher key")
    
    configs_to_try = [
        # SQLCipher 4.x defaults (Signal Desktop uses this)
        {
            "name": "SQLCipher 4.x defaults",
            "key_format": "raw",
            "cipher_page_size": 4096,
            "kdf_iter": 256000,
            "cipher_hmac_algorithm": "HMAC_SHA512",
            "cipher_kdf_algorithm": "PBKDF2_HMAC_SHA512",
        },
        # Signal-specific settings
        {
            "name": "Signal Desktop settings",
            "key_format": "raw", 
            "cipher_page_size": 4096,
            "kdf_iter": 64000,
            "cipher_hmac_algorithm": "HMAC_SHA512",
            "cipher_kdf_algorithm": "PBKDF2_HMAC_SHA512",
        },
        # SQLCipher 3.x compatibility
        {
            "name": "SQLCipher 3.x compat",
            "key_format": "raw",
            "cipher_compatibility": 3,
        },
        # SQLCipher 4.x compatibility
        {
            "name": "SQLCipher 4.x compat",
            "key_format": "raw",
            "cipher_compatibility": 4,
        },
        # Try as passphrase
        {
            "name": "As passphrase (4.x)",
            "key_format": "passphrase",
            "cipher_page_size": 4096,
            "kdf_iter": 256000,
        },
    ]
    
    for cfg in configs_to_try:
        name = cfg.pop("name")
        result = try_open_db(db_path, encrypted_key, cfg)
        status = "✅ SUCCESS" if result else "❌ Failed"
        print(f"  {name}: {status}")
        if result:
            print(f"\n🎉 Database opened with config: {cfg}")
            return
    
    # Try 2: Check if there's a key file or other sources
    print("\n" + "-" * 60)
    print("Attempt 2: Look for key in other locations")
    
    signal_dir = Path(config_path).parent
    possible_key_files = [
        signal_dir / "key",
        signal_dir / "db.key", 
        signal_dir / "sql" / "key",
        signal_dir / "sql" / "db.key",
    ]
    
    for kf in possible_key_files:
        if kf.exists():
            print(f"  Found: {kf}")
            key_content = kf.read_text().strip()
            print(f"  Content: {key_content[:40]}...")
        else:
            print(f"  Not found: {kf}")
    
    # Try 3: Check for WAL file (might have unencrypted data)
    print("\n" + "-" * 60)
    print("Attempt 3: Check WAL file for readable data")
    
    wal_path = Path(db_path).with_suffix(".sqlite-wal")
    if wal_path.exists():
        print(f"  WAL file exists: {wal_path} ({wal_path.stat().st_size} bytes)")
        # Try to find readable strings
        try:
            content = wal_path.read_bytes()
            # Look for common Signal table names
            markers = [b"conversations", b"messages", b"sqlite_master"]
            for marker in markers:
                if marker in content:
                    print(f"  Found '{marker.decode()}' in WAL - data might be partially readable!")
        except Exception as e:
            print(f"  Could not read WAL: {e}")
    else:
        print(f"  No WAL file found")
    
    # Summary
    print("\n" + "=" * 60)
    print("RESULT: Could not decrypt database")
    print("\nThe database is encrypted with a key that was protected by")
    print("Windows DPAPI on the original machine. To decrypt, you need to:")
    print("")
    print("1. Run decrypt_key.ps1 on the ORIGINAL Windows account")
    print("   where Signal Desktop was installed")
    print("")
    print("2. Or use signal-export tool while Signal Desktop is running:")
    print("   pip install signal-export")
    print("   signalexport ~/signal-backup")
    print("")
    print("3. Or manually export chat from Signal Desktop (copy/paste)")


if __name__ == "__main__":
    main()
