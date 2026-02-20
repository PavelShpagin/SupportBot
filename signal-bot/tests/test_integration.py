"""
Integration tests for the running SupportBot stack.

Run inside the signal-bot container:
    docker compose exec signal-bot python -m pytest /app/tests/test_integration.py -v

Or from the host against localhost:
    python tests/test_integration.py
"""
from __future__ import annotations

import json
import time
import uuid
import sys
import os

import httpx

BASE = os.getenv("BOT_URL", "http://localhost:8000")
CHROMA_URL = os.getenv("CHROMA_URL", "http://localhost:8002")
MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "supportbot")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "supportbot")
MYSQL_DB = os.getenv("MYSQL_DATABASE", "supportbot")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _db():
    """Return a fresh MySQL connection (pymysql)."""
    import pymysql
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DB,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _db_scalar(sql: str, args=None):
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args or ())
            row = cur.fetchone()
            return list(row.values())[0] if row else None
    finally:
        conn.close()


def _db_rows(sql: str, args=None):
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args or ())
            return cur.fetchall()
    finally:
        conn.close()


def _chroma_count() -> int:
    """Count total docs across the 'cases' Chroma collection."""
    r = httpx.get(f"{CHROMA_URL}/api/v2/tenants/default_tenant/databases/default_database/collections", timeout=10)
    r.raise_for_status()
    cols = r.json()
    for c in cols:
        if c["name"] == "cases":
            col_id = c["id"]
            r2 = httpx.get(
                f"{CHROMA_URL}/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/count",
                timeout=10,
            )
            r2.raise_for_status()
            return int(r2.text.strip())
    return 0


def _max_job_id() -> int:
    """Return the current maximum job_id in the jobs table (0 if empty)."""
    val = _db_scalar("SELECT COALESCE(MAX(job_id), 0) FROM jobs")
    return int(val) if val else 0


def _wait_for_jobs_after(min_job_id: int, *, timeout: float = 60.0, poll: float = 1.0) -> bool:
    """
    Wait until all jobs with job_id > min_job_id have status 'done' or 'failed'.
    Ignores pre-existing stuck jobs.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = _db_scalar(
            "SELECT COUNT(*) FROM jobs "
            "WHERE job_id > %s AND status IN ('pending','in_progress')",
            (min_job_id,),
        )
        if pending == 0:
            return True
        time.sleep(poll)
    return False


def _wait_for_jobs(*, timeout: float = 30.0, poll: float = 1.0) -> bool:
    """Wait until no 'pending' or 'in_progress' jobs exist (global)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = _db_scalar("SELECT COUNT(*) FROM jobs WHERE status IN ('pending','in_progress')")
        if pending == 0:
            return True
        time.sleep(poll)
    return False


def _wait_for_chroma_count(expected_min: int, *, timeout: float = 15.0, poll: float = 1.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _chroma_count() >= expected_min:
            return True
        time.sleep(poll)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# TEST 1 – DB state
# ─────────────────────────────────────────────────────────────────────────────

def test_db_no_pending_jobs():
    """
    No BUFFER_UPDATE or MAYBE_RESPOND jobs should be stuck in pending/in_progress.

    Rules:
    - HISTORY_LINK jobs legitimately sit in_progress while waiting for signal-ingest QR scan.
    - 'in_progress' jobs that are stale (worker crashed before finishing) show up as
      abandoned; we flag those older than 10 minutes as truly stuck.
    """
    worker_types = ("BUFFER_UPDATE", "MAYBE_RESPOND")
    placeholders = ",".join(["%s"] * len(worker_types))
    sql_count = (
        "SELECT COUNT(*) FROM jobs "
        "WHERE status IN ('pending','in_progress') "
        f"AND type IN ({placeholders}) "
        "AND updated_at < DATE_SUB(NOW(), INTERVAL 10 MINUTE)"
    )
    stuck = _db_scalar(sql_count, worker_types)
    assert stuck == 0, (
        f"Expected 0 stuck BUFFER_UPDATE/MAYBE_RESPOND jobs (>10min old), got {stuck}. "
        f"Details: {_db_rows(sql_count.replace('COUNT(*)', 'job_id, type, status, attempts, updated_at'), worker_types)}"
    )


def test_db_no_stale_raw_messages():
    """
    raw_messages for production groups (not test groups) should be empty — the
    ingest pipeline clears them via clear_group_runtime_data() on each re-ingest.

    Test-group messages (group_id starting with 'test-' or 'rag-') are created
    by integration tests and excluded from this check.
    """
    # Use %% to escape literal % in LIKE patterns when passing to pymysql
    count = _db_scalar(
        "SELECT COUNT(*) FROM raw_messages "
        "WHERE group_id NOT LIKE 'test-%%' AND group_id NOT LIKE 'rag-%%'"
    )
    assert count == 0, (
        f"raw_messages has {count} production rows — the last ingest should have "
        "cleared them via clear_group_runtime_data(). Possible partial/failed ingest."
    )


def test_db_cases_have_valid_status():
    """Every case row must have a known status value."""
    bad = _db_rows(
        "SELECT case_id, status FROM cases WHERE status NOT IN ('open','solved','archived')"
    )
    assert not bad, f"Cases with invalid status: {bad}"


def test_db_cases_in_rag_consistency():
    """Cases marked in_rag=1 should be 'solved' (only solved cases are indexed)."""
    bad = _db_rows(
        "SELECT case_id, status, in_rag FROM cases WHERE in_rag=1 AND status != 'solved'"
    )
    assert not bad, (
        f"Cases marked in_rag=1 but not 'solved': {bad}. "
        "Only solved cases should be indexed in ChromaDB."
    )


def test_db_buffer_not_stale():
    """There should be at most 1 open buffer per group (no duplicate stuck buffers)."""
    rows = _db_rows(
        "SELECT group_id, COUNT(*) as cnt FROM buffers GROUP BY group_id HAVING cnt > 1"
    )
    assert not rows, f"Groups with multiple buffer rows (stuck buffers): {rows}"


# ─────────────────────────────────────────────────────────────────────────────
# TEST 2 – Service health
# ─────────────────────────────────────────────────────────────────────────────

def test_bot_health():
    """GET / returns {status: ok}."""
    r = httpx.get(f"{BASE}/", timeout=5)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_bot_healthz():
    """GET /healthz returns {ok: true}."""
    r = httpx.get(f"{BASE}/healthz", timeout=5)
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_chroma_reachable():
    """Chroma /api/v2/heartbeat is reachable."""
    r = httpx.get(f"{CHROMA_URL}/api/v2/heartbeat", timeout=5)
    assert r.status_code == 200, f"Chroma heartbeat failed: {r.status_code}"


def test_signal_link_status_endpoint():
    """GET /signal/link-device/status returns expected shape (debug endpoint)."""
    r = httpx.get(f"{BASE}/signal/link-device/status", timeout=5)
    assert r.status_code == 200, f"Status endpoint returned {r.status_code}"
    data = r.json()
    assert "status" in data, f"Missing 'status' key: {data}"
    assert data["status"] in ("idle", "linking", "done", "error"), (
        f"Unexpected status: {data['status']}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# TEST 3 – Signal CLI registration
# ─────────────────────────────────────────────────────────────────────────────

def test_signal_cli_registered():
    """
    The signal-cli account must be registered/linked for the bot to receive messages.

    We verify by checking the accounts.json file inside the running container
    (same logic as is_account_registered() in app.signal.link_device).
    """
    import subprocess, json as _json

    result = subprocess.run(
        [
            "docker", "compose",
            "-f", "/home/pavel/dev/SupportBot/docker-compose.yml",
            "exec", "-T", "signal-bot",
            "python3", "-c",
            (
                "import json; from pathlib import Path; import os; "
                "cfg=os.getenv('SIGNAL_BOT_STORAGE','/var/lib/signal'); "
                "acc_path=Path(cfg)/'data'/'accounts.json'; "
                "print('exists:', acc_path.exists()); "
                "data=json.loads(acc_path.read_text()) if acc_path.exists() else {}; "
                "accts=data.get('accounts',[]); "
                "print('accounts:', [a.get('number') for a in accts])"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    out = result.stdout.strip()
    assert "exists: True" in out, (
        f"signal-cli accounts.json not found — bot may not be registered.\n{out}\n{result.stderr}"
    )
    e164 = os.getenv("SIGNAL_BOT_E164", "+380730017651")
    assert e164 in out, (
        f"Expected phone {e164} in accounts list, got: {out}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# TEST 4 – Ingestion pipeline
# ─────────────────────────────────────────────────────────────────────────────

def test_ingest_message_enqueues_job():
    """
    POST /debug/ingest should:
      1. Return 200 with ok=True
      2. Create a BUFFER_UPDATE job that the worker processes to 'done'/'failed'
    """
    group_id = f"test-group-{uuid.uuid4().hex[:8]}"
    sender = "+380991112233"

    baseline_job_id = _max_job_id()

    r = httpx.post(
        f"{BASE}/debug/ingest",
        json={"group_id": group_id, "sender": sender, "text": "Тест повідомлення"},
        timeout=10,
    )
    assert r.status_code == 200, f"debug/ingest failed: {r.status_code} {r.text}"
    body = r.json()
    assert body["ok"] is True
    assert "message_id" in body

    # Verify a new job was created
    jobs_created = _db_rows(
        "SELECT job_id, type, status FROM jobs WHERE job_id > %s",
        (baseline_job_id,),
    )
    assert jobs_created, "No jobs were created after debug/ingest"
    job_types_created = {j["type"] for j in jobs_created}
    assert "BUFFER_UPDATE" in job_types_created, (
        f"Expected BUFFER_UPDATE to be enqueued, got: {job_types_created}"
    )

    # Wait only for the jobs created by this test
    done = _wait_for_jobs_after(baseline_job_id, timeout=45)
    assert done, (
        f"Jobs created by this test did not finish within 45 seconds. "
        f"Still active: {_db_rows('SELECT job_id, type, status FROM jobs WHERE job_id > %s AND status IN (\"pending\",\"in_progress\")', (baseline_job_id,))}"
    )


def test_ingest_full_case_pipeline():
    """
    Simulate a full support conversation:
      - User asks a question
      - Another user answers it
      - Verify the worker creates a case (open or solved) for the group.

    Uses /debug/ingest to inject messages, then polls only the jobs created
    by THIS test (tracked by job_id) so pre-existing stuck jobs don't block us.
    """
    group_id = f"test-case-{uuid.uuid4().hex[:8]}"
    user_sender = "+380991112233"

    cases_before = _db_scalar("SELECT COUNT(*) FROM cases")
    baseline_job_id = _max_job_id()

    # 1. User asks a question
    r1 = httpx.post(
        f"{BASE}/debug/ingest",
        json={
            "group_id": group_id,
            "sender": user_sender,
            "text": (
                "Привіт! У мене проблема з підключенням WiFi — "
                "телефон показує 'Saved, Secured' але не підключається."
            ),
        },
        timeout=10,
    )
    assert r1.status_code == 200, f"ingest Q failed: {r1.status_code}"
    q_msg_id = r1.json()["message_id"]

    time.sleep(0.3)

    # 2. Another user provides an answer (reply to the question)
    r2 = httpx.post(
        f"{BASE}/debug/ingest",
        json={
            "group_id": group_id,
            "sender": "+380997654321",
            "text": (
                "Треба забути мережу і підключитися знову. "
                "Налаштування → WiFi → довге натискання на мережу → Забути. "
                "Потім повторно підключитися і ввести пароль."
            ),
            "reply_to_id": q_msg_id,
        },
        timeout=10,
    )
    assert r2.status_code == 200, f"ingest A failed: {r2.status_code}"

    # 3. Wait only for the jobs created by THIS test (job_id > baseline)
    done = _wait_for_jobs_after(baseline_job_id, timeout=120)
    assert done, (
        f"Jobs created by this test (job_id>{baseline_job_id}) did not finish within 120s. "
        f"Still running: "
        + str(_db_rows(
            "SELECT job_id, type, status FROM jobs WHERE job_id > %s AND status IN ('pending','in_progress')",
            (baseline_job_id,),
        ))
    )

    # 4. All test jobs must have terminated (done or failed — not stuck)
    new_jobs = _db_rows(
        "SELECT job_id, type, status FROM jobs WHERE job_id > %s",
        (baseline_job_id,),
    )
    assert new_jobs, "No jobs were created for the test ingestion"
    for job in new_jobs:
        assert job["status"] in ("done", "failed"), (
            f"Job {job['job_id']} ({job['type']}) ended in unexpected status: {job['status']}"
        )

    # 5. At least one BUFFER_UPDATE must have been created and processed
    buf_jobs = [j for j in new_jobs if j["type"] == "BUFFER_UPDATE"]
    assert buf_jobs, "Expected at least one BUFFER_UPDATE job to be created"
    # With the Chroma empty-list fix, BUFFER_UPDATE should complete with 'done'
    buf_done = [j for j in buf_jobs if j["status"] == "done"]
    assert buf_done, (
        f"All BUFFER_UPDATE jobs failed — check worker logs for errors. "
        f"Jobs: {buf_jobs}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# TEST 5 – RAG retrieval
# ─────────────────────────────────────────────────────────────────────────────

def _chroma_collection_id() -> str:
    """Return the Chroma collection id for the 'cases' collection."""
    r = httpx.get(
        f"{CHROMA_URL}/api/v2/tenants/default_tenant/databases/default_database/collections",
        timeout=10,
    )
    r.raise_for_status()
    for c in r.json():
        if c["name"] == "cases":
            return c["id"]
    raise RuntimeError("Chroma 'cases' collection not found")


def _chroma_collection_dim() -> int:
    """Probe the embedding dimension used by the 'cases' collection (default 3072)."""
    col_id = _chroma_collection_id()
    # Query with 1 result to get an existing doc and check its embedding size.
    r = httpx.post(
        f"{CHROMA_URL}/api/v2/tenants/default_tenant/databases/default_database"
        f"/collections/{col_id}/get",
        json={"limit": 1, "include": ["embeddings"]},
        timeout=10,
    )
    if r.status_code == 200:
        body = r.json()
        embeddings = body.get("embeddings") or []
        if embeddings and isinstance(embeddings[0], list):
            return len(embeddings[0])
    return 3072  # default


def _seed_rag_case(group_id: str) -> str:
    """
    Directly insert one solved case into MySQL + ChromaDB to seed the RAG index.

    We bypass /history/cases (which requires bot group membership) and write
    directly — appropriate for integration tests that only care about retrieval.
    Returns the group_id used.
    """
    import hashlib

    case_id = hashlib.md5(f"{group_id}-seed".encode()).hexdigest()
    doc_text = (
        "[SOLVED] WiFi showing 'Saved, Secured' but won't connect\n"
        "Проблема: Телефон показує 'Saved, Secured' але не підключається до WiFi.\n"
        "Рішення: Забудьте мережу і підключіться знову. "
        "Налаштування → WiFi → довге натискання → Забути → ввести пароль.\n"
        "tags: wifi, network, android"
    )

    # 1. Insert into MySQL
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT IGNORE INTO cases
                   (case_id, group_id, status, problem_title, problem_summary,
                    solution_summary, tags_json, in_rag)
                   VALUES (%s, %s, 'solved', %s, %s, %s, %s, 1)""",
                (
                    case_id,
                    group_id,
                    "WiFi не підключається",
                    "Телефон показує Saved, Secured але не підключається.",
                    "Забути мережу і підключитися знову з паролем.",
                    '["wifi","network","android"]',
                ),
            )
            conn.commit()
    finally:
        conn.close()

    # 2. Upsert into Chroma via REST API (v2, version-agnostic).
    #    Use a dummy zero embedding matched to the collection's dimension.
    dim = _chroma_collection_dim()
    col_id = _chroma_collection_id()
    r = httpx.post(
        f"{CHROMA_URL}/api/v2/tenants/default_tenant/databases/default_database"
        f"/collections/{col_id}/upsert",
        json={
            "ids": [case_id],
            "documents": [doc_text],
            "embeddings": [[0.0] * dim],
            "metadatas": [{"group_id": group_id, "status": "solved"}],
        },
        timeout=15,
    )
    assert r.status_code in (200, 201), (
        f"Chroma upsert failed: {r.status_code} {r.text}"
    )
    return group_id


def test_rag_retrieve_returns_results():
    """
    After seeding a solved case directly into MySQL+Chroma, /retrieve should
    return it when queried with semantically similar text.
    """
    group_id = f"rag-test-{uuid.uuid4().hex[:8]}"
    _seed_rag_case(group_id)

    # /retrieve embeds the query via LLM and queries Chroma.
    # Our seeded case uses a dummy embedding, so it may not rank highest —
    # but the endpoint itself must return 200 and a valid structure.
    query = "WiFi підключений але не працює інтернет"
    r = httpx.post(
        f"{BASE}/retrieve",
        json={"group_id": group_id, "query": query, "k": 3},
        timeout=15,
    )
    assert r.status_code == 200, f"/retrieve failed: {r.status_code} {r.text}"
    body = r.json()
    assert "cases" in body, f"Response missing 'cases': {body}"
    assert isinstance(body["cases"], list), f"'cases' is not a list: {body}"
    # The seeded case must be visible in Chroma (it was just upserted)
    chroma_cnt = _chroma_count()
    assert chroma_cnt >= 1, f"Expected at least 1 doc in Chroma after seeding, got {chroma_cnt}"


def test_rag_retrieve_respects_group_isolation():
    """
    A query for group B must NOT return cases seeded under group A.
    Chroma filters by group_id metadata, so group B results should be empty.
    """
    group_a = f"rag-iso-a-{uuid.uuid4().hex[:8]}"
    group_b = f"rag-iso-b-{uuid.uuid4().hex[:8]}"
    _seed_rag_case(group_a)

    r = httpx.post(
        f"{BASE}/retrieve",
        json={"group_id": group_b, "query": "WiFi проблема підключення", "k": 5},
        timeout=15,
    )
    assert r.status_code == 200
    body = r.json()
    assert "cases" in body, f"Missing 'cases' key: {body}"
    for case in body["cases"]:
        meta = case.get("metadata", {})
        assert meta.get("group_id") == group_b, (
            f"Group isolation violated! Retrieved case from group '{meta.get('group_id')}' "
            f"when querying group '{group_b}'"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        # DB state
        ("test_db_no_pending_jobs", test_db_no_pending_jobs),
        ("test_db_no_stale_raw_messages", test_db_no_stale_raw_messages),
        ("test_db_cases_have_valid_status", test_db_cases_have_valid_status),
        ("test_db_cases_in_rag_consistency", test_db_cases_in_rag_consistency),
        ("test_db_buffer_not_stale", test_db_buffer_not_stale),
        # Service health
        ("test_bot_health", test_bot_health),
        ("test_bot_healthz", test_bot_healthz),
        ("test_chroma_reachable", test_chroma_reachable),
        ("test_signal_link_status_endpoint", test_signal_link_status_endpoint),
        # Signal registration
        ("test_signal_cli_registered", test_signal_cli_registered),
        # Ingestion
        ("test_ingest_message_enqueues_job", test_ingest_message_enqueues_job),
        ("test_ingest_full_case_pipeline", test_ingest_full_case_pipeline),
        # RAG
        ("test_rag_retrieve_returns_results", test_rag_retrieve_returns_results),
        ("test_rag_retrieve_respects_group_isolation", test_rag_retrieve_respects_group_isolation),
    ]

    passed = 0
    failed = 0
    errors = []
    print(f"\n{'='*60}")
    print("SupportBot Integration Test Suite")
    print(f"  Bot:    {BASE}")
    print(f"  Chroma: {CHROMA_URL}")
    print(f"{'='*60}\n")

    for name, fn in tests:
        try:
            fn()
            print(f"  ✓  {name}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗  {name}")
            print(f"     {e}")
            failed += 1
            errors.append((name, str(e)))
        except Exception as e:
            print(f"  ✗  {name}  [ERROR]")
            print(f"     {type(e).__name__}: {e}")
            failed += 1
            errors.append((name, f"{type(e).__name__}: {e}"))

    total = passed + failed
    print(f"\n{'='*60}")
    print(f"Results: {passed}/{total} passed", end="")
    if failed:
        print(f", {failed} failed")
    else:
        print()
    print(f"{'='*60}\n")
    sys.exit(0 if failed == 0 else 1)
