#!/usr/bin/env python3
import sqlite3
import json

db_path = '/home/signal/.config/Signal/sql/db.sqlite'
key_path = '/home/signal/.config/Signal/config.json'

with open(key_path) as f:
    key = json.load(f).get('key', '')

conn = sqlite3.connect(db_path)
conn.execute(f"PRAGMA key = \"x'{key}'\";")

# List tables
tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print('Tables:', [t[0] for t in tables])

# Check if reactions table exists
if any(t[0] == 'reactions' for t in tables):
    cols = conn.execute('PRAGMA table_info(reactions)').fetchall()
    print('Reactions columns:', [c[1] for c in cols])
    count = conn.execute('SELECT COUNT(*) FROM reactions').fetchone()[0]
    print('Reactions count:', count)
    if count > 0:
        rows = conn.execute('SELECT * FROM reactions LIMIT 5').fetchall()
        for r in rows:
            print('  ', r)
else:
    print('No reactions table found')
