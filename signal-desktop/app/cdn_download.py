"""
Direct Signal CDN attachment downloader — no CDP required.

Signal Desktop stores the following fields in each attachment's JSON for every
message it syncs, including historical ones from before the device was linked:

    cdnKey     — object key on Signal's CDN
    cdnNumber  — 1, 2, or 3 (determines hostname)
    key        — base64-encoded 64 bytes: [AES-256 key (32)] + [HMAC-SHA256 key (32)]
    digest     — base64 SHA-256 hash of the *encrypted* file (for integrity)
    size       — plaintext file size (may be null for very old messages)
    contentType, fileName, ...

We read these from the SQLite DB (already accessible via db_reader._open_db),
authenticate with the CDN using the account credentials stored in the ``items``
table, download the encrypted blob, verify its integrity, and decrypt it.

Decrypted files are cached in ``{signal_data_dir}/cdn-cache/{cdnKey}`` so we
never download the same attachment twice within a session.

Signal attachment wire format (AES-256-CBC):
    [ IV : 16 bytes ]
    [ AES-256-CBC ciphertext : variable ]
    [ HMAC-SHA256 over (IV + ciphertext) : 32 bytes ]

CDN hosts:
    1 → https://cdn.signal.org/attachments/{cdnKey}
    2 → https://cdn2.signal.org/attachments/{cdnKey}
    3 → https://cdn3.signal.org/{cdnKey}

Auth (all CDN tiers):
    Authorization: Basic base64("{uuid}.{deviceId}:{password}")
"""

from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
import json
import logging
import os
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def get_signal_credentials(db_conn) -> dict:
    """Read CDN auth credentials from Signal Desktop's ``items`` table.

    Returns a dict with keys: ``uuid``, ``password``, ``deviceId``, ``number``.
    Any missing value is returned as an empty string / 1.
    """
    wanted = {"uuid", "password", "deviceId", "number"}
    result: dict = {"uuid": "", "password": "", "deviceId": 1, "number": ""}

    try:
        rows = db_conn.execute(
            "SELECT id, json FROM items WHERE id IN ('uuid','password','deviceId','number')"
        ).fetchall()
    except Exception as e:
        log.warning("Could not read items table: %s", e)
        return result

    for row_id, row_json in rows:
        try:
            val = json.loads(row_json).get("value")
        except (json.JSONDecodeError, TypeError, AttributeError):
            val = None
        if val is None:
            continue
        if row_id == "deviceId":
            try:
                result["deviceId"] = int(val)
            except (ValueError, TypeError):
                result["deviceId"] = 1
        elif row_id in result:
            result[row_id] = str(val)

    return result


def _make_auth_header(credentials: dict) -> str:
    uuid = credentials.get("uuid") or credentials.get("number") or ""
    device_id = credentials.get("deviceId", 1)
    password = credentials.get("password", "")
    raw = f"{uuid}.{device_id}:{password}"
    return "Basic " + base64.b64encode(raw.encode()).decode()


# ---------------------------------------------------------------------------
# CDN download
# ---------------------------------------------------------------------------

_CDN_HOSTS = {
    1: "https://cdn.signal.org/attachments/{key}",
    2: "https://cdn2.signal.org/attachments/{key}",
    3: "https://cdn3.signal.org/{key}",
}


def download_from_cdn(
    cdn_key: str,
    cdn_number: int,
    credentials: dict,
    *,
    timeout: int = 60,
) -> bytes:
    """Download raw (encrypted) attachment bytes from Signal's CDN.

    Raises ``httpx.HTTPError`` on network / HTTP errors.
    """
    cdn_number = cdn_number if cdn_number in _CDN_HOSTS else 2
    url = _CDN_HOSTS[cdn_number].format(key=cdn_key)
    auth = _make_auth_header(credentials)

    log.debug("Downloading attachment from CDN %d: %s", cdn_number, url)
    with httpx.Client(timeout=timeout) as client:
        r = client.get(url, headers={"Authorization": auth})
        r.raise_for_status()
        return r.content


# ---------------------------------------------------------------------------
# Decryption
# ---------------------------------------------------------------------------

def decrypt_attachment(encrypted_data: bytes, key_b64: str) -> bytes:
    """Decrypt a Signal attachment.

    Wire format: [IV:16][AES-256-CBC ciphertext][HMAC-SHA256:32]
    Key layout (64 bytes): [AES key:32][HMAC key:32]

    Raises ``ValueError`` on HMAC mismatch or bad padding.
    """
    key = base64.b64decode(key_b64)
    if len(key) != 64:
        raise ValueError(f"Expected 64-byte key, got {len(key)}")

    aes_key = key[:32]
    mac_key = key[32:64]

    if len(encrypted_data) < 16 + 16 + 32:  # IV + 1 AES block + MAC
        raise ValueError(f"Encrypted data too short: {len(encrypted_data)} bytes")

    iv = encrypted_data[:16]
    mac = encrypted_data[-32:]
    ciphertext = encrypted_data[16:-32]

    # Verify HMAC-SHA256 over IV + ciphertext
    to_mac = encrypted_data[:-32]
    computed_mac = _hmac.new(mac_key, to_mac, hashlib.sha256).digest()
    if not _hmac.compare_digest(computed_mac, mac):
        raise ValueError("HMAC verification failed — data corrupt or wrong key")

    # Decrypt AES-256-CBC
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as _padding

    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()

    unpadder = _padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


# ---------------------------------------------------------------------------
# Combined: download + decrypt + cache
# ---------------------------------------------------------------------------

def download_and_decrypt(
    cdn_key: str,
    cdn_number: Optional[int],
    key_b64: str,
    signal_data_dir: str,
    *,
    credentials: Optional[dict] = None,
    db_conn=None,
) -> bytes:
    """Download, decrypt, and cache a Signal attachment.

    On first call, fetches from CDN and writes to
    ``{signal_data_dir}/cdn-cache/{cdn_key}``.  Subsequent calls return the
    cached file without hitting the network.

    Supply either ``credentials`` (a dict from ``get_signal_credentials``) or
    ``db_conn`` (an open SQLCipher connection) so that credentials can be
    looked up.  If neither is provided, the download is attempted without auth
    (will likely fail for private attachments).
    """
    if not cdn_key:
        raise ValueError("cdn_key is empty")
    if not key_b64:
        raise ValueError("key_b64 is empty — cannot decrypt")

    cache_dir = Path(signal_data_dir) / "cdn-cache"
    cache_file = cache_dir / cdn_key

    # Return cached plaintext if available
    if cache_file.exists():
        log.debug("CDN cache hit for %s", cdn_key)
        return cache_file.read_bytes()

    # Resolve credentials
    if credentials is None:
        if db_conn is not None:
            credentials = get_signal_credentials(db_conn)
        else:
            credentials = {}
            log.warning("No credentials provided — CDN request may fail")

    cdn_number = cdn_number if isinstance(cdn_number, int) else 2

    # Download
    encrypted = download_from_cdn(cdn_key, cdn_number, credentials)

    # Decrypt
    plaintext = decrypt_attachment(encrypted, key_b64)

    # Cache
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_bytes(plaintext)
    log.debug("CDN download cached: %s (%d bytes)", cdn_key, len(plaintext))

    return plaintext
