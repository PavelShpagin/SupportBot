import mysql.connector, os, json

conn = mysql.connector.connect(
    host='127.0.0.1', port=3306,
    user=os.environ.get('MYSQL_USER', 'supportbot'),
    password=os.environ.get('MYSQL_PASSWORD', 'supportbot'),
    database=os.environ.get('MYSQL_DB', 'supportbot'),
)
cur = conn.cursor(dictionary=True)

# 1. Count by status
cur.execute("SELECT status, count(*) as n FROM cases GROUP BY status ORDER BY n DESC")
print("=== Cases by status ===")
for r in cur.fetchall():
    print(f"  {r['status']}: {r['n']}")

# 2. All cases - title + status + solution_summary snippet
cur.execute("""
    SELECT case_id, status, problem_title,
           LEFT(problem_summary, 80) as problem_short,
           LEFT(solution_summary, 80) as solution_short,
           created_at
    FROM cases
    ORDER BY created_at DESC
""")
cases = cur.fetchall()
print(f"\n=== All {len(cases)} cases ===")
for c in cases:
    print(f"\n[{c['case_id']}] status={c['status']}")
    print(f"  title:    {c['problem_title']}")
    print(f"  problem:  {c['problem_short']}")
    print(f"  solution: {c['solution_short']}")

# 3. Detect duplicates by similar title
from collections import defaultdict
titles = defaultdict(list)
for c in cases:
    t = (c['problem_title'] or '').lower().strip()
    titles[t].append(c['case_id'])

print("\n=== Duplicate titles ===")
found_dups = False
for title, ids in titles.items():
    if len(ids) > 1:
        print(f"  '{title}': {ids}")
        found_dups = True
if not found_dups:
    print("  (none by exact title match)")

conn.close()
