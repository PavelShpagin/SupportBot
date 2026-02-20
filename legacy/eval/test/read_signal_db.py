#!/usr/bin/env python3
"""
Read Signal Desktop database using decrypted key.

Prerequisites:
1. Run decrypt_key.ps1 on original Windows machine to get the key
2. Provide the SQLCipher key in ONE of these ways:
   - Set env var `SIGNAL_KEY_HEX` (64 hex chars for 32-byte key), OR put it in `.env` (repo root)
   - Place signal_key.txt in test/secrets/signal_key.txt (preferred) or test/data/signal_key.txt
3. Have the Signal backup extracted in test/data/extracted/Signal1/

Usage:
    python test/read_signal_db.py

This will export messages to test/data/signal_messages.json
"""

import json
from dataclasses import dataclass
import sys
import os
from pathlib import Path
from typing import Iterable, Optional

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


# Optional convenience for local runs: load repo-root .env (does not override).
_maybe_load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR / "data"
SECRETS_DIR = SCRIPT_DIR / "secrets"
KEY_PATHS = [
    SECRETS_DIR / "signal_key.txt",
    DATA_DIR / "signal_key.txt",
]
DB_PATH = DATA_DIR / "extracted" / "Signal1" / "sql" / "db.sqlite"
OUTPUT_PATH = DATA_DIR / "signal_messages.json"


def _table_columns(conn, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    # row format: (cid, name, type, notnull, dflt_value, pk)
    return [r[1] for r in rows]


def _pick_first(existing: set[str], candidates: Iterable[str]) -> Optional[str]:
    for c in candidates:
        if c in existing:
            return c
    return None


def _row_get(row, key: str, idx: int):
    """Get value by column name if possible, otherwise by index."""
    try:
        return row[key]  # type: ignore[index]
    except Exception:
        return row[idx]  # type: ignore[index]


@dataclass(frozen=True)
class MessageColumns:
    id_col: str
    conversation_id_col: str
    ts_col: str
    body_col: str
    type_col: Optional[str]
    sender_col: Optional[str]


def _detect_message_columns(conn) -> MessageColumns:
    cols = set(_table_columns(conn, "messages"))

    id_col = _pick_first(cols, ["id", "_id"])
    conv_col = _pick_first(cols, ["conversationId", "conversation_id", "conversation"])
    ts_col = _pick_first(cols, ["sent_at", "sentAt", "timestamp", "received_at", "receivedAt"])
    body_col = _pick_first(cols, ["body", "message", "text"])
    type_col = _pick_first(cols, ["type", "messageType"])
    sender_col = _pick_first(cols, ["sourceUuid", "source_uuid", "sourceServiceId", "source", "sender"])

    missing = [("id", id_col), ("conversationId", conv_col), ("timestamp", ts_col), ("body", body_col)]
    missing = [name for name, val in missing if not val]
    if missing:
        raise RuntimeError(f"Unsupported messages schema; missing columns: {missing}. Found: {sorted(cols)}")

    return MessageColumns(
        id_col=id_col or "id",
        conversation_id_col=conv_col or "conversationId",
        ts_col=ts_col or "sent_at",
        body_col=body_col or "body",
        type_col=type_col,
        sender_col=sender_col,
    )


@dataclass(frozen=True)
class AttachmentColumns:
    message_id_col: str
    attachment_id_col: Optional[str]
    content_type_col: Optional[str]
    file_name_col: Optional[str]
    size_col: Optional[str]
    path_col: Optional[str]
    caption_col: Optional[str]


def _detect_attachment_columns(conn) -> AttachmentColumns:
    cols = set(_table_columns(conn, "message_attachments"))
    msg_id_col = _pick_first(cols, ["messageId", "message_id", "message"])
    if not msg_id_col:
        raise RuntimeError(f"Unsupported message_attachments schema; missing messageId. Found: {sorted(cols)}")

    return AttachmentColumns(
        message_id_col=msg_id_col,
        attachment_id_col=_pick_first(cols, ["attachmentId", "attachment_id", "id"]),
        content_type_col=_pick_first(cols, ["contentType", "content_type", "mimeType", "mime_type"]),
        file_name_col=_pick_first(cols, ["fileName", "file_name", "name"]),
        size_col=_pick_first(cols, ["size", "fileSize", "file_size"]),
        path_col=_pick_first(cols, ["path", "downloadPath", "localBackupPath", "thumbnailPath"]),
        caption_col=_pick_first(cols, ["caption"]),
    )


def main():
    print("Signal Database Reader")
    print("=" * 50)
    
    # Check key file
    key_hex = (os.environ.get("SIGNAL_KEY_HEX") or "").strip()
    key_path_used: Path | None = None
    if not key_hex:
        for p in KEY_PATHS:
            if p.exists():
                key_hex = p.read_text().strip()
                key_path_used = p
                break
    if not key_hex:
        print("ERROR: Signal key not provided.")
        print("\nProvide it in one of these ways:")
        print("- Set env var SIGNAL_KEY_HEX (64 hex chars for 32-byte key)")
        print("- Put key in one of these files:")
        for p in KEY_PATHS:
            print(f"  - {p}")
        print("\nTo get the key:")
        print("1) On Windows with Signal Desktop, run: test/decrypt_key.ps1 (pwsh) or test/decrypt_key_win.py (python)")
        print("2) Copy Desktop\\signal_key.txt into test/secrets/signal_key.txt")
        sys.exit(1)
    
    # Check database
    if not DB_PATH.exists():
        print(f"ERROR: Database not found: {DB_PATH}")
        print("\nExtract the Signal backup first:")
        print("  cd test/data && unzip Signal1-*.zip -d extracted/")
        sys.exit(1)
    
    # Read key
    if key_path_used:
        print(f"Key file: {key_path_used}")
    print(f"Key: {key_hex[:16]}... ({len(key_hex)} chars)")
    
    if len(key_hex) != 64:
        print(f"WARNING: Expected 64 char key (32 bytes), got {len(key_hex)}")
    
    # Try to import SQLCipher-enabled sqlite module
    try:
        import sqlcipher3 as sqlcipher
        print("Using sqlcipher3 (from sqlcipher3-binary)")
    except ImportError:
        print("\nERROR: sqlcipher3 not installed")
        print("Install with: pip install sqlcipher3-binary")
        sys.exit(1)
    
    # Connect to database
    print(f"\nOpening database: {DB_PATH}")
    conn = sqlcipher.connect(str(DB_PATH))
    # Dict-like rows
    try:
        conn.row_factory = sqlcipher.Row  # type: ignore[attr-defined]
    except Exception:
        pass
    
    # Signal Desktop uses SQLCipher 4 settings
    try:
        conn.execute("PRAGMA cipher_compatibility = 4;")
        conn.execute("PRAGMA cipher_page_size = 4096;")
        conn.execute(f"PRAGMA key = \"x'{key_hex}'\";")
        
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
        rows = conn.execute("SELECT id, name, type, profileName FROM conversations").fetchall()
        print(f"Total conversations: {len(rows)}")
        group_count = 0
        for row in rows:
            cid = _row_get(row, "id", 0)
            name = _row_get(row, "name", 1)
            ctype = _row_get(row, "type", 2)
            profile = _row_get(row, "profileName", 3)

            display_name = name or profile or f"(unnamed {ctype})"
            conversations[cid] = {"name": display_name, "type": ctype}

            if str(ctype).lower() == "group":
                group_count += 1
        print(f"Group conversations: {group_count}")
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
    
    cols = _detect_message_columns(conn)
    print("Detected messages schema:")
    print(f"- id: {cols.id_col}")
    print(f"- conversation_id: {cols.conversation_id_col}")
    print(f"- timestamp: {cols.ts_col}")
    print(f"- body: {cols.body_col}")
    print(f"- type: {cols.type_col or '(none)'}")
    print(f"- sender: {cols.sender_col or '(none)'}")
    print("")

    # Preload attachment metadata so we can represent attachment-only messages.
    attachments_by_message: dict[str, list[dict]] = {}
    try:
        a_cols_for_messages = _detect_attachment_columns(conn)
        ma_cols_set = set(_table_columns(conn, "message_attachments"))
        has_conv_in_ma = "conversationId" in ma_cols_set
        has_type_in_ma = "attachmentType" in ma_cols_set

        select_parts = [f"ma.{a_cols_for_messages.message_id_col} as message_id"]
        if has_type_in_ma:
            select_parts.append("ma.attachmentType as attachment_type")
        if a_cols_for_messages.content_type_col:
            select_parts.append(f"ma.{a_cols_for_messages.content_type_col} as content_type")
        if a_cols_for_messages.file_name_col:
            select_parts.append(f"ma.{a_cols_for_messages.file_name_col} as file_name")
        if a_cols_for_messages.size_col:
            select_parts.append(f"ma.{a_cols_for_messages.size_col} as size")
        if a_cols_for_messages.path_col:
            select_parts.append(f"ma.{a_cols_for_messages.path_col} as path")
        if a_cols_for_messages.caption_col:
            select_parts.append(f"ma.{a_cols_for_messages.caption_col} as caption")

        if target_group:
            if has_conv_in_ma:
                att_sql = f"""
                    SELECT {", ".join(select_parts)}
                    FROM message_attachments ma
                    WHERE ma.conversationId = ?
                """
                att_rows = conn.execute(att_sql, (target_group,)).fetchall()
            else:
                # Fallback: join through messages if message_attachments lacks conversationId
                att_sql = f"""
                    SELECT {", ".join(select_parts)}
                    FROM message_attachments ma
                    JOIN messages m ON ma.{a_cols_for_messages.message_id_col} = m.{cols.id_col}
                    WHERE m.{cols.conversation_id_col} = ?
                """
                att_rows = conn.execute(att_sql, (target_group,)).fetchall()

            for r in att_rows:
                row_d = dict(r)
                mid = str(row_d.get("message_id") or "").strip()
                if not mid:
                    continue
                d = {
                    "attachment_type": row_d.get("attachment_type") if has_type_in_ma else None,
                    "content_type": row_d.get("content_type") if a_cols_for_messages.content_type_col else None,
                    "file_name": row_d.get("file_name") if a_cols_for_messages.file_name_col else None,
                    "size": row_d.get("size") if a_cols_for_messages.size_col else None,
                    "path": row_d.get("path") if a_cols_for_messages.path_col else None,
                    "caption": row_d.get("caption") if a_cols_for_messages.caption_col else None,
                }
                attachments_by_message.setdefault(mid, []).append({k: v for k, v in d.items() if v not in (None, "")})
    except Exception as e:
        print(f"NOTE: Could not preload attachments per message: {e}")

    messages = []
    sender_select = f"m.{cols.sender_col} as sender" if cols.sender_col else "NULL as sender"
    type_select = f"m.{cols.type_col} as type" if cols.type_col else "NULL as type"

    # Export *all* messages for target group (including attachment-only messages).
    where_clause = f"WHERE m.{cols.conversation_id_col} = ?" if target_group else ""
    query = f"""
        SELECT
            m.{cols.id_col} as id,
            m.{cols.conversation_id_col} as conversation_id,
            m.{cols.ts_col} as timestamp,
            m.{cols.body_col} as body,
            {type_select},
            {sender_select}
        FROM messages m
        {where_clause}
        ORDER BY m.{cols.ts_col}
    """
    
    try:
        cur = conn.execute(query, (target_group,)) if target_group else conn.execute(query)
        for row in cur:
            conv_id = _row_get(row, "conversation_id", 1)
            conv_info = conversations.get(conv_id, {})
            msg_id = str(_row_get(row, "id", 0))
            body_raw = _row_get(row, "body", 3) or ""
            atts = attachments_by_message.get(msg_id, [])

            parts = []
            if body_raw and str(body_raw).strip():
                parts.append(str(body_raw).strip())
            for a in atts:
                ct = a.get("content_type", "")
                fn = a.get("file_name", "")
                sz = a.get("size", "")
                cap = a.get("caption", "")
                path = a.get("path", "")
                line = f"[ATTACHMENT {ct}".strip()
                if fn:
                    line += f" file={fn}"
                if sz:
                    line += f" size={sz}"
                if path:
                    line += f" path={path}"
                line += "]"
                parts.append(line)
                if cap:
                    parts.append(f"caption: {cap}")

            body_combined = "\n".join(parts).strip()
            if not body_combined:
                # Completely empty message (rare); keep for ordering but mark as empty.
                body_combined = ""

            messages.append(
                {
                    "id": msg_id,
                    "conversation_id": conv_id,
                    "conversation_name": conv_info.get("name", ""),
                    "timestamp": _row_get(row, "timestamp", 2),
                    "body": body_combined,
                    "body_raw": body_raw,
                    "type": _row_get(row, "type", 4),
                    "sender": (_row_get(row, "sender", 5) or ""),
                    "attachments": atts,
                }
            )
    except Exception as e:
        print(f"  Error reading messages: {e}")
    
    print(f"  Exported {len(messages)} messages")
    
    # Attachments / multimedia summary
    attachment_summary: dict = {}
    try:
        if not target_group:
            raise RuntimeError("Target group was not detected; cannot summarize attachments.")

        a_cols = _detect_attachment_columns(conn)
        print("\n" + "=" * 50)
        print("ATTACHMENTS / MULTIMEDIA")
        print("=" * 50)
        print("Detected message_attachments schema:")
        print(f"- message_id: {a_cols.message_id_col}")
        print(f"- attachment_id: {a_cols.attachment_id_col or '(none)'}")
        print(f"- content_type: {a_cols.content_type_col or '(none)'}")
        print(f"- file_name: {a_cols.file_name_col or '(none)'}")
        print(f"- size: {a_cols.size_col or '(none)'}")

        total_sql = f"""
            SELECT COUNT(*)
            FROM message_attachments ma
            JOIN messages m ON ma.{a_cols.message_id_col} = m.{cols.id_col}
            WHERE m.{cols.conversation_id_col} = ?
        """
        total_attachments = int(conn.execute(total_sql, (target_group,)).fetchone()[0])
        print(f"Total attachments in target group: {total_attachments}")

        content_counts: dict[str, int] = {}
        images = videos = audios = others = 0
        if a_cols.content_type_col:
            ct_sql = f"""
                SELECT ma.{a_cols.content_type_col} as ct, COUNT(*) as n
                FROM message_attachments ma
                JOIN messages m ON ma.{a_cols.message_id_col} = m.{cols.id_col}
                WHERE m.{cols.conversation_id_col} = ?
                GROUP BY ct
                ORDER BY n DESC
            """
            for ct, n in conn.execute(ct_sql, (target_group,)).fetchall():
                if ct is None:
                    continue
                content_counts[str(ct)] = int(n)

            def _sum_prefix(prefix: str) -> int:
                return sum(v for k, v in content_counts.items() if k.lower().startswith(prefix))

            images = _sum_prefix("image/")
            videos = _sum_prefix("video/")
            audios = _sum_prefix("audio/")
            others = max(total_attachments - (images + videos + audios), 0)
            print(f"By type: images={images}, videos={videos}, audio={audios}, other/unknown={others}")

        sample_sql = f"""
            SELECT ma.*, m.{cols.ts_col} as msg_ts
            FROM message_attachments ma
            JOIN messages m ON ma.{a_cols.message_id_col} = m.{cols.id_col}
            WHERE m.{cols.conversation_id_col} = ?
            ORDER BY m.{cols.ts_col} DESC
            LIMIT 10
        """
        samples = []
        for row in conn.execute(sample_sql, (target_group,)).fetchall():
            sample = {
                "message_id": _row_get(row, a_cols.message_id_col, 0),
                "timestamp": _row_get(row, "msg_ts", -1),
            }
            if a_cols.content_type_col:
                sample["content_type"] = _row_get(row, a_cols.content_type_col, 0)
            if a_cols.file_name_col:
                sample["file_name"] = _row_get(row, a_cols.file_name_col, 0)
            if a_cols.size_col:
                sample["size"] = _row_get(row, a_cols.size_col, 0)
            samples.append(sample)

        attachment_summary = {
            "total_attachments": total_attachments,
            "content_type_counts": content_counts,
            "counts": {"images": images, "videos": videos, "audios": audios, "other_or_unknown": others},
            "samples": samples,
        }
    except Exception as e:
        attachment_summary = {"error": str(e)}

    # Save to JSON (note: file is in test/data/ which is gitignored)
    output = {
        "target_group": target_group,
        "target_group_name": (conversations.get(target_group, {}) or {}).get("name", ""),
        "messages": messages,
        "attachment_summary": attachment_summary,
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
