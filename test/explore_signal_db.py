#!/usr/bin/env python3
"""Explore Signal Desktop SQLite database structure."""

import sqlite3
import json
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "extracted" / "Signal1" / "sql" / "db.sqlite"


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # List all tables
    print("=== TABLES ===")
    tables = cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    for t in tables:
        print(f"  {t[0]}")

    # Check for messages table
    print("\n=== MESSAGES TABLE SCHEMA ===")
    try:
        schema = cur.execute("PRAGMA table_info(messages)").fetchall()
        for col in schema:
            print(f"  {col[1]:30} {col[2]}")
    except Exception as e:
        print(f"  Error: {e}")

    # Check for conversations/groups
    print("\n=== CONVERSATIONS TABLE SCHEMA ===")
    try:
        schema = cur.execute("PRAGMA table_info(conversations)").fetchall()
        for col in schema:
            print(f"  {col[1]:30} {col[2]}")
    except Exception as e:
        print(f"  Error: {e}")

    # List groups/conversations
    print("\n=== CONVERSATIONS (Groups) ===")
    try:
        convs = cur.execute("""
            SELECT id, type, name, profileName, groupId 
            FROM conversations 
            WHERE type = 'group' OR groupId IS NOT NULL
            LIMIT 20
        """).fetchall()
        for c in convs:
            print(f"  ID: {c['id'][:40]}... | Name: {c['name'] or c['profileName'] or 'N/A'}")
    except Exception as e:
        print(f"  Error: {e}")

    # Find the specific group
    print("\n=== LOOKING FOR 'Техпідтримка Академія СтабХ' ===")
    try:
        convs = cur.execute("""
            SELECT id, type, name, profileName, groupId 
            FROM conversations 
            WHERE name LIKE '%Техпідтримка%' OR name LIKE '%СтабХ%' OR name LIKE '%Академія%'
        """).fetchall()
        for c in convs:
            print(f"  ID: {c['id']}")
            print(f"  Name: {c['name']}")
            print(f"  Type: {c['type']}")
            print(f"  GroupId: {c['groupId']}")
            
            # Count messages in this conversation
            msg_count = cur.execute(
                "SELECT COUNT(*) FROM messages WHERE conversationId = ?", 
                (c['id'],)
            ).fetchone()[0]
            print(f"  Message count: {msg_count}")
            print()
    except Exception as e:
        print(f"  Error: {e}")

    # Sample messages
    print("\n=== SAMPLE MESSAGES (first 5) ===")
    try:
        msgs = cur.execute("""
            SELECT id, conversationId, type, body, sent_at, source, sourceServiceId
            FROM messages 
            WHERE body IS NOT NULL AND body != ''
            ORDER BY sent_at DESC
            LIMIT 5
        """).fetchall()
        for m in msgs:
            body = (m['body'] or '')[:100]
            print(f"  [{m['sent_at']}] {m['source'] or 'N/A'}: {body}...")
    except Exception as e:
        print(f"  Error: {e}")

    conn.close()


if __name__ == "__main__":
    main()
