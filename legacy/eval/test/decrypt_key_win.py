#!/usr/bin/env python3
"""
Decrypt Signal Desktop SQLCipher key on Windows.

This script fixes the common mistake of attempting to DPAPI-decrypt
config.json "encryptedKey" directly. That value is usually in Chromium/Electron
"v10" (AES-GCM) format and must be decrypted using the DPAPI-protected master key
stored in "Local State" (os_crypt.encrypted_key).

Run this on the SAME Windows user account where Signal Desktop is installed.

Usage (PowerShell):
  python .\\decrypt_key_win.py

  # Or point at an extracted Signal folder containing:
  #   - config.json
  #   - Local State
  python .\\decrypt_key_win.py "C:\\path\\to\\extracted\\Signal1"

Output:
  %USERPROFILE%\\Desktop\\signal_key.txt
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys
from pathlib import Path


def _dpapi_unprotect(data: bytes) -> bytes:
    """Decrypt DPAPI blob for CurrentUser using WinAPI (no pywin32 needed)."""
    import ctypes
    from ctypes import wintypes

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL

    in_buf = ctypes.create_string_buffer(data)
    blob_in = DATA_BLOB(len(data), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_byte)))
    blob_out = DATA_BLOB()

    if not crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    ):
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        # LocalFree returns NULL on success.
        kernel32.LocalFree(blob_out.pbData)


def _is_ascii_hex(b: bytes) -> bool:
    hexdigits = b"0123456789abcdefABCDEF"
    return all(ch in hexdigits for ch in b)


def main() -> int:
    if sys.platform != "win32":
        print("ERROR: Run this on Windows (not WSL/Linux).")
        return 2

    if len(sys.argv) > 1:
        signal_dir = Path(sys.argv[1]).expanduser()
    else:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            print("ERROR: APPDATA environment variable not set.")
            return 2
        signal_dir = Path(appdata) / "Signal"
    config_path = signal_dir / "config.json"
    local_state_path = signal_dir / "Local State"

    print("Signal Desktop Key Decryptor (Python)")
    print("====================================")
    print(f"Signal dir : {signal_dir}")
    print(f"config.json: {config_path}")
    print(f"Local State: {local_state_path}")
    print()

    if not config_path.exists():
        print("ERROR: config.json not found. Is Signal Desktop installed for this user?")
        return 1
    if not local_state_path.exists():
        print("ERROR: Local State not found. Is Signal Desktop installed for this user?")
        return 1

    # Load Local State
    local_state = json.loads(local_state_path.read_text(encoding="utf-8"))
    enc_key_b64 = local_state.get("os_crypt", {}).get("encrypted_key")
    if not enc_key_b64:
        print("ERROR: Local State missing os_crypt.encrypted_key")
        return 1

    enc_key_all = base64.b64decode(enc_key_b64)
    print(f"Local State encrypted_key: {len(enc_key_all)} bytes (b64 decoded)")
    if not enc_key_all.startswith(b"DPAPI"):
        print(f"ERROR: Unexpected Local State encrypted_key prefix: {enc_key_all[:8]!r}")
        return 1

    enc_key_dpapi = enc_key_all[5:]
    master_key = _dpapi_unprotect(enc_key_dpapi)
    print(f"Master key: {len(master_key)} bytes, hex prefix={master_key.hex()[:16]}...")

    # Load config.json encryptedKey (hex) and decrypt v10
    config = json.loads(config_path.read_text(encoding="utf-8"))
    encrypted_key_hex = config.get("encryptedKey")
    if not encrypted_key_hex:
        print("ERROR: config.json missing encryptedKey")
        return 1

    try:
        encrypted_key_bytes = binascii.unhexlify(encrypted_key_hex.strip())
    except Exception as e:
        print(f"ERROR: encryptedKey is not valid hex: {e}")
        return 1

    print(f"config.json encryptedKey: {len(encrypted_key_bytes)} bytes (hex decoded)")
    prefix = encrypted_key_bytes[:3]
    print(f"encryptedKey prefix: {prefix!r} (expected b'v10' or b'v11')")

    if prefix not in (b"v10", b"v11"):
        print("ERROR: encryptedKey does not look like v10/v11 AES-GCM.")
        print("If you're sure this is an older Signal Desktop, you might have a DPAPI-only encryptedKey.")
        return 1

    if len(encrypted_key_bytes) < 3 + 12 + 16:
        print("ERROR: encryptedKey too short to be AES-GCM.")
        return 1

    nonce = encrypted_key_bytes[3:15]
    cipher_and_tag = encrypted_key_bytes[15:]

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        print("ERROR: Missing dependency 'cryptography'.")
        print("Install it with:")
        print("  pip install cryptography")
        return 1

    aesgcm = AESGCM(master_key)
    try:
        plaintext = aesgcm.decrypt(nonce, cipher_and_tag, None)
    except Exception as e:
        print(f"ERROR: AES-GCM decrypt failed: {e}")
        print("Most likely causes:")
        print("- different Windows user account")
        print("- Signal was reinstalled (Local State/config changed)")
        return 1

    print(f"Decrypted plaintext: {len(plaintext)} bytes")

    desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
    out_path = desktop / "signal_key.txt"

    if _is_ascii_hex(plaintext):
        key_str = plaintext.decode("ascii")
        print("\nSUCCESS! SQLCipher key (ASCII hex):\n")
        print(key_str)
        out_path.write_text(key_str, encoding="ascii")
    else:
        key_hex = plaintext.hex()
        print("\nSUCCESS! SQLCipher key (raw bytes as hex):\n")
        print(key_hex)
        out_path.write_text(key_hex, encoding="ascii")

    print(f"\nSaved to: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

