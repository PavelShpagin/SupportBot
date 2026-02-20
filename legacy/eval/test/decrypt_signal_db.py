#!/usr/bin/env python3
"""
Decrypt Signal Desktop SQLite database using DPAPI.
Must be run on Windows where Signal Desktop was used.
"""

import json
import sys
from pathlib import Path

# This script should be run on Windows
try:
    import win32crypt
except ImportError:
    print("ERROR: This script must be run on Windows with pywin32 installed.")
    print("Run: pip install pywin32")
    sys.exit(1)

try:
    import pysqlcipher3.dbapi2 as sqlcipher
except ImportError:
    print("ERROR: pysqlcipher3 not installed.")
    print("Run: pip install pysqlcipher3")
    sys.exit(1)


def decrypt_key(encrypted_key_hex: str) -> bytes:
    """Decrypt the Signal key using Windows DPAPI."""
    encrypted_key = bytes.fromhex(encrypted_key_hex)
    # Signal uses DPAPI with CRYPTPROTECT_UI_FORBIDDEN flag
    decrypted = win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)
    return decrypted[1]


def main():
    base_path = Path(__file__).parent / "data" / "extracted" / "Signal1"
    config_path = base_path / "config.json"
    db_path = base_path / "sql" / "db.sqlite"

    if not config_path.exists():
        print(f"ERROR: config.json not found at {config_path}")
        sys.exit(1)

    if not db_path.exists():
        print(f"ERROR: db.sqlite not found at {db_path}")
        sys.exit(1)

    # Load encrypted key
    with open(config_path) as f:
        config = json.load(f)

    encrypted_key_hex = config.get("encryptedKey")
    if not encrypted_key_hex:
        print("ERROR: encryptedKey not found in config.json")
        sys.exit(1)

    print(f"Encrypted key length: {len(encrypted_key_hex)} hex chars")

    # Decrypt key using DPAPI
    try:
        key = decrypt_key(encrypted_key_hex)
        print(f"Decrypted key length: {len(key)} bytes")
    except Exception as e:
        print(f"ERROR: Failed to decrypt key: {e}")
        print("This may mean the database was encrypted on a different Windows user/machine.")
        sys.exit(1)

    # Open database with SQLCipher
    conn = sqlcipher.connect(str(db_path))
    conn.execute(f"PRAGMA key=\"x'{key.hex()}'\"")
    conn.execute("PRAGMA cipher_page_size = 4096")
    conn.execute("PRAGMA kdf_iter = 64000")
    conn.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA512")
    conn.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512")

    # Test connection
    try:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        print(f"\nFound {len(tables)} tables:")
        for t in tables:
            print(f"  {t[0]}")
    except Exception as e:
        print(f"ERROR: Failed to read database: {e}")
        sys.exit(1)

    conn.close()
    print("\nDatabase decryption successful!")


if __name__ == "__main__":
    main()
