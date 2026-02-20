#!/usr/bin/env python3
"""
Inspect decrypted Signal Desktop DB schema and attachment metadata.

This is a local debugging helper to understand where multimedia lives:
- which columns exist in `messages`, `message_attachments`, `attachment_downloads`
- whether there are local file paths / digests we can map to files in the export

Usage:
  source .venv/bin/activate
  python test/inspect_signal_db.py
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

PRINT_VALUES = os.environ.get("INSPECT_PRINT_VALUES", "").strip().lower() in {"1", "true", "yes", "y"}


def _pick_first(existing: set[str], candidates: Iterable[str]) -> Optional[str]:
    for c in candidates:
        if c in existing:
            return c
    return None


def _read_key_hex() -> str:
    here = Path(__file__).parent
    candidates = [
        os.environ.get("SIGNAL_KEY_HEX", "").strip(),
        (here / "secrets" / "signal_key.txt").read_text().strip()
        if (here / "secrets" / "signal_key.txt").exists()
        else "",
        (here / "data" / "signal_key.txt").read_text().strip()
        if (here / "data" / "signal_key.txt").exists()
        else "",
    ]
    for c in candidates:
        if c:
            return c
    raise SystemExit("No key found. Put it in test/secrets/signal_key.txt or set SIGNAL_KEY_HEX.")


def _open_conn():
    import sqlcipher3 as sqlcipher

    key_hex = _read_key_hex()
    db_path = Path(__file__).parent / "data" / "extracted" / "Signal1" / "sql" / "db.sqlite"
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    conn = sqlcipher.connect(str(db_path))
    conn.row_factory = sqlcipher.Row
    conn.execute("PRAGMA cipher_compatibility = 4;")
    conn.execute("PRAGMA cipher_page_size = 4096;")
    conn.execute(f"PRAGMA key = \"x'{key_hex}'\";")
    # quick sanity
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    return conn


def _table_columns(conn, table: str) -> list[str]:
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _print_table(conn, table: str, sample_sql: str, sample_args=()):
    cols = _table_columns(conn, table)
    print(f"\n== {table} ==")
    print(f"columns({len(cols)}): {cols}")
    rows = conn.execute(sample_sql, sample_args).fetchall()
    print(f"sample rows: {len(rows)}")
    if not PRINT_VALUES:
        print("values: (redacted; set INSPECT_PRINT_VALUES=1 to print limited samples)")
        return

    def _redact(k: str, v):
        lk = k.lower()
        if any(x in lk for x in ["key", "digest", "iv", "mac", "hash", "secret"]):
            if v in (None, ""):
                return v
            return "<redacted>"
        return v

    for i, r in enumerate(rows[:3], 1):
        # print only a few keys to avoid huge output
        d = dict(r)
        keys = list(d.keys())[:20]
        trimmed = {k: _redact(k, d[k]) for k in keys}
        print(f"  row {i}: {trimmed}")


def main() -> None:
    conn = _open_conn()

    # Detect the group conversation id
    conv_rows = conn.execute("SELECT id, name, type, profileName FROM conversations").fetchall()
    conversations = []
    for r in conv_rows:
        name = r["name"] or r["profileName"] or ""
        conversations.append((r["id"], r["type"], name))

    kw = ["техпідтримка", "академія", "стабх"]
    target = None
    for cid, ctype, name in conversations:
        if str(ctype).lower() == "group" and any(k in (name or "").lower() for k in kw):
            target = (cid, name)
            break

    if not target:
        raise SystemExit("Target group not found by keywords.")

    group_id, group_name = target
    print(f"Target group: {group_name} ({group_id})")

    # Messages schema
    _print_table(conn, "messages", "SELECT * FROM messages LIMIT 1")

    # Attachments schemas
    _print_table(
        conn,
        "message_attachments",
        """
        SELECT ma.*
        FROM message_attachments ma
        JOIN messages m ON ma.messageId = m.id
        WHERE m.conversationId = ?
        LIMIT 3
        """,
        (group_id,),
    )

    _print_table(conn, "attachment_downloads", "SELECT * FROM attachment_downloads LIMIT 3")

    # Try to find obvious local-path columns in message_attachments
    ma_cols = set(_table_columns(conn, "message_attachments"))
    path_col = _pick_first(ma_cols, ["path", "localPath", "downloadPath", "filePath", "relativePath"])
    if path_col:
        print(f"\nFound potential path column in message_attachments: {path_col}")
        r = conn.execute(
            f"""
            SELECT ma.{path_col} as p
            FROM message_attachments ma
            JOIN messages m ON ma.messageId = m.id
            WHERE m.conversationId = ? AND ma.{path_col} IS NOT NULL AND ma.{path_col} != ''
            LIMIT 5
            """,
            (group_id,),
        ).fetchall()
        sample_paths = [x["p"] for x in r if x["p"]]
        print("Sample paths:", sample_paths)

        # Try to resolve to real files in the extracted export
        export_root = Path(__file__).parent / "data" / "extracted" / "Signal1"
        candidate_bases = [
            export_root / "attachments.noindex",
            export_root / "Attachments.noindex",
            export_root / "attachments",
            export_root,
        ]

        existing_bases = [p for p in candidate_bases if p.exists()]
        print("\nAttachment file resolution:")
        print("Candidate bases that exist:", [str(p.relative_to(export_root)) if p != export_root else "." for p in existing_bases])

        def _norm(rel: str) -> Path:
            return Path(rel.replace("\\\\", "/").replace("\\", "/"))

        found_any = False
        for rel_s in sample_paths[:10]:
            rel = _norm(str(rel_s))
            found = []
            for base in existing_bases:
                full = base / rel
                if full.exists():
                    found.append(full)
            if found:
                found_any = True
                print(f"  ✅ {rel} -> {found[0]}")
            else:
                print(f"  ❌ {rel} -> not found in export")

        if not found_any:
            print("\nNOTE: No attachment payload files were found in the extracted export.")
            print("This can happen if the export is incomplete (e.g. split archives) or attachments were not included.")
    else:
        print("\nNo obvious local-path column found in message_attachments.")

    conn.close()


if __name__ == "__main__":
    main()

