#!/usr/bin/env python3
"""
Decrypt Signal Desktop V2 attachments from `attachments.noindex`.

Why this exists:
- Signal Desktop stores attachment payloads encrypted on disk.
- DB table `message_attachments` contains `path`, `size` (plaintext size), and `localKey` (base64).
- With those, we can decrypt using the same format as Signal Desktop's AttachmentCrypto V2:
    file = IV(16) + AES-256-CBC(ciphertext) + HMAC-SHA256(mac, over IV+ciphertext)(32)
    plaintext = AES-CBC-decrypt(ciphertext) then trim to `size` bytes.

Inputs:
- SQLCipher DB (already decrypted via key): test/data/extracted/Signal1/sql/db.sqlite
- Ciphertext files:                test/data/extracted/Signal1/attachments.noindex/<path>
- SQLCipher key: set SIGNAL_KEY_HEX or put in test/secrets/signal_key.txt

Outputs (gitignored under test/data/):
- Decrypted files: test/data/decrypted_attachments/
- Index JSON:      test/data/decrypted_attachments_index.json

Usage (WSL, recommended):
  source .venv/bin/activate
  python test/decrypt_attachments_v2.py

Controls (env vars):
- DECRYPT_GROUP_KEYWORDS: comma-separated keywords to pick group by name (default: техпідтримка,академія,стабх)
- DECRYPT_TYPES: comma-separated content-type prefixes to decrypt (default: image/)
- DECRYPT_MAX_FILES: max files to decrypt (default: 25)
- DECRYPT_MAX_BYTES: skip attachments larger than this many bytes (default: 15000000)
"""

from __future__ import annotations

import base64
import json
import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


IV_LEN = 16
MAC_LEN = 32
AES_KEY_LEN = 32
KEY_SET_LEN = 64


def _maybe_load_dotenv(dotenv_path: Path) -> None:
    """
    Load key=value pairs from .env, stripping CRLF, without overriding existing env.
    """
    if not dotenv_path.exists():
        return
    for raw in dotenv_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip("\r")
        if not k:
            continue
        if (v.startswith("'") and v.endswith("'")) or (v.startswith('"') and v.endswith('"')):
            v = v[1:-1]
        os.environ.setdefault(k, v)


def _read_key_hex(repo: Path) -> str:
    # Prefer env var (can come from .env via _maybe_load_dotenv).
    key = (os.environ.get("SIGNAL_KEY_HEX") or "").strip()
    if key:
        return key
    for p in [
        repo / "test" / "secrets" / "signal_key.txt",
        repo / "test" / "data" / "signal_key.txt",
    ]:
        if p.exists():
            return p.read_text(encoding="utf-8", errors="ignore").strip()
    raise SystemExit("Missing SQLCipher key. Set SIGNAL_KEY_HEX or put it in test/secrets/signal_key.txt")


def _open_conn(repo: Path):
    import sqlcipher3 as sqlcipher

    key_hex = _read_key_hex(repo)
    db_path = repo / "test" / "data" / "extracted" / "Signal1" / "sql" / "db.sqlite"
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    conn = sqlcipher.connect(str(db_path))
    conn.row_factory = sqlcipher.Row
    conn.execute("PRAGMA cipher_compatibility = 4;")
    conn.execute("PRAGMA cipher_page_size = 4096;")
    conn.execute(f"PRAGMA key = \"x'{key_hex}'\";")
    # Sanity
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    return conn


def _norm_rel_path(p: str) -> str:
    s = (p or "").strip().strip('"').strip("'").replace("\\", "/").lstrip("/")
    parts = [x for x in s.split("/") if x not in ("", ".", "..")]
    return "/".join(parts)


def _safe_name(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    # Windows reserved + keep it short
    s = re.sub(r"[\\/:*?\"<>|]", "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:180]


def _ext_for_content_type(ct: str) -> str:
    ct = (ct or "").lower().strip()
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "application/pdf": ".pdf",
        "application/zip": ".zip",
        "application/x-zip-compressed": ".zip",
        "text/plain": ".txt",
        "text/csv": ".csv",
    }
    return mapping.get(ct, ".bin")


def _decrypt_v2(*, ciphertext: bytes, keys_b64: str, size: int) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if size < 0:
        raise ValueError("size must be >= 0")
    if len(ciphertext) < IV_LEN + MAC_LEN + 16:
        raise ValueError("ciphertext too short")

    keys = base64.b64decode(keys_b64)
    if len(keys) != KEY_SET_LEN:
        raise ValueError(f"localKey must be {KEY_SET_LEN} bytes, got {len(keys)}")

    aes_key = keys[:AES_KEY_LEN]
    mac_key = keys[AES_KEY_LEN:]

    iv = ciphertext[:IV_LEN]
    mac = ciphertext[-MAC_LEN:]
    body = ciphertext[:-MAC_LEN]
    enc = ciphertext[IV_LEN:-MAC_LEN]

    expected = hmac.new(mac_key, body, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, mac):
        raise ValueError("bad MAC (wrong key or corrupted file)")

    if len(enc) % 16 != 0:
        raise ValueError("ciphertext length not multiple of AES block size")

    decryptor = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(enc) + decryptor.finalize()
    return padded[:size]


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    _maybe_load_dotenv(repo / ".env")

    attachments_root = repo / "test" / "data" / "extracted" / "Signal1" / "attachments.noindex"
    if not attachments_root.exists():
        raise SystemExit(f"Missing attachments folder: {attachments_root}")

    out_dir = repo / "test" / "data" / "decrypted_attachments"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_index = repo / "test" / "data" / "decrypted_attachments_index.json"

    prefixes = [p.strip() for p in (os.environ.get("DECRYPT_TYPES") or "image/").split(",") if p.strip()]
    max_files = int(os.environ.get("DECRYPT_MAX_FILES") or "25")
    max_bytes = int(os.environ.get("DECRYPT_MAX_BYTES") or "15000000")
    kw = [k.strip().lower() for k in (os.environ.get("DECRYPT_GROUP_KEYWORDS") or "техпідтримка,академія,стабх").split(",") if k.strip()]

    conn = _open_conn(repo)
    try:
        # Pick target group by keywords
        convs = conn.execute("SELECT id, name, type, profileName FROM conversations").fetchall()
        target_group_id = None
        target_group_name = ""
        for r in convs:
            ctype = str(r["type"] or "").lower()
            if ctype != "group":
                continue
            name = (r["name"] or r["profileName"] or "").strip()
            if not name:
                continue
            low = name.lower()
            if any(k in low for k in kw):
                target_group_id = r["id"]
                target_group_name = name
                break
        if not target_group_id:
            raise SystemExit(f"Could not find target group by keywords: {kw}")

        print(f"Target group: {target_group_name} ({target_group_id})")
        print(f"Decrypting content types starting with: {prefixes}")
        print(f"Max files: {max_files}, max bytes each: {max_bytes}")
        print("")

        # Query attachments for this conversation
        rows = conn.execute(
            """
            SELECT
              ma.messageId as message_id,
              ma.orderInMessage as order_in_message,
              ma.contentType as content_type,
              ma.fileName as file_name,
              ma.size as size,
              ma.path as path,
              ma.localKey as local_key,
              ma.version as version,
              ma.plaintextHash as plaintext_hash
            FROM message_attachments ma
            WHERE ma.conversationId = ?
            ORDER BY ma.sentAt ASC, ma.orderInMessage ASC
            """,
            (target_group_id,),
        ).fetchall()

        print(f"Attachment rows in group: {len(rows)}")

        results: List[Dict[str, Any]] = []
        done = 0
        skipped = 0
        failed = 0

        for r in rows:
            ct = (r["content_type"] or "").strip()
            if prefixes and not any(ct.lower().startswith(p.lower()) for p in prefixes):
                skipped += 1
                continue

            rel = _norm_rel_path(str(r["path"] or ""))
            if not rel:
                skipped += 1
                continue

            size = int(r["size"] or 0)
            if max_bytes and size > max_bytes:
                skipped += 1
                continue

            version = int(r["version"] or 0)
            local_key = (r["local_key"] or "").strip()
            if version != 2 or not local_key:
                skipped += 1
                continue

            cipher_path = attachments_root / rel
            if not cipher_path.exists():
                failed += 1
                results.append(
                    {
                        "message_id": r["message_id"],
                        "order_in_message": r["order_in_message"],
                        "content_type": ct,
                        "path": rel,
                        "status": "missing_ciphertext",
                    }
                )
                continue

            # Choose output name
            file_name = _safe_name(str(r["file_name"] or ""))
            ext = _ext_for_content_type(ct)
            if file_name:
                out_name = file_name
                if not out_name.lower().endswith(ext) and "." not in Path(out_name).name:
                    out_name += ext
            else:
                mid = str(r["message_id"] or "msg")
                oi = int(r["order_in_message"] or 0)
                out_name = f"{mid}_{oi:02d}{ext}"

            # Put into a type folder for convenience
            bucket = (ct.split("/", 1)[0] or "other").lower()
            out_bucket = out_dir / bucket
            out_bucket.mkdir(parents=True, exist_ok=True)

            out_path = out_bucket / out_name
            # Avoid overwrite collisions
            if out_path.exists():
                stem = out_path.stem
                suffix = out_path.suffix
                for j in range(2, 1000):
                    cand = out_bucket / f"{stem}__{j}{suffix}"
                    if not cand.exists():
                        out_path = cand
                        break

            try:
                blob = cipher_path.read_bytes()
                plain = _decrypt_v2(ciphertext=blob, keys_b64=local_key, size=size)
                out_path.write_bytes(plain)
                ok = True
                # Optional: verify plaintextHash if present
                ph = (r["plaintext_hash"] or "").strip()
                ph_ok = None
                if ph:
                    got = hashlib.sha256(plain).hexdigest()
                    ph_ok = got == ph
                results.append(
                    {
                        "message_id": r["message_id"],
                        "order_in_message": r["order_in_message"],
                        "content_type": ct,
                        "cipher_rel_path": rel,
                        "out_path": str(out_path.relative_to(repo)),
                        "size": size,
                        "plaintext_hash_ok": ph_ok,
                        "status": "ok",
                    }
                )
                done += 1
            except Exception as e:
                failed += 1
                results.append(
                    {
                        "message_id": r["message_id"],
                        "order_in_message": r["order_in_message"],
                        "content_type": ct,
                        "cipher_rel_path": rel,
                        "status": "failed",
                        "error": str(e),
                    }
                )

            if max_files and done >= max_files:
                break

        index = {
            "group_id": target_group_id,
            "group_name": target_group_name,
            "types_prefixes": prefixes,
            "max_files": max_files,
            "max_bytes": max_bytes,
            "decrypted_ok": done,
            "skipped": skipped,
            "failed": failed,
            "results": results,
        }
        out_index.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        print("")
        print(f"Decrypted ok: {done}, skipped: {skipped}, failed: {failed}")
        print(f"Wrote index: {out_index}")
        print(f"Output dir:  {out_dir}")
        return 0 if failed == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

