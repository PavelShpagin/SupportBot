"""
Unit tests for upsert_case and confirm_cases_by_evidence_ts.

These tests use an in-memory SQLite database (no MySQL, no Docker needed)
with a thin compatibility shim so the queries_mysql functions run unchanged.

Run with:
    pytest tests/test_db_cases.py -v
"""
from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, List, Optional
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# sys.path: make signal-bot importable
# ---------------------------------------------------------------------------

_BOT_DIR = str(ROOT / "signal-bot")
if _BOT_DIR not in sys.path:
    sys.path.insert(0, _BOT_DIR)

# Stub heavy deps
for _lib in ("chromadb", "google", "google.generativeai", "mysql", "mysql.connector", "mysql.connector.errors"):
    if _lib not in sys.modules:
        sys.modules[_lib] = MagicMock()


# ---------------------------------------------------------------------------
# SQLite shim – makes queries_mysql functions work with SQLite
# ---------------------------------------------------------------------------

class _SQLiteCursor:
    """Thin wrapper that translates %s placeholders → ? for SQLite."""

    def __init__(self, cur: sqlite3.Cursor):
        self._cur = cur
        self.rowcount = 0

    def execute(self, sql: str, params=()) -> None:
        sql_lite = sql.replace("%s", "?")
        # SQLite doesn't support MySQL's multi-table UPDATE ... JOIN syntax;
        # we rewrite it to a correlated subquery form.
        if "JOIN case_evidence ce ON ce.case_id = c.case_id" in sql_lite:
            sql_lite = """
            UPDATE cases
            SET closed_emoji = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE case_id IN (
                SELECT ce.case_id FROM case_evidence ce
                WHERE ce.message_id = ?
            )
            AND status IN ('solved', 'open')
            AND closed_emoji IS NULL
            """
            # params is (emoji, message_id) — order matches rewritten query
        self._cur.execute(sql_lite, params)
        self.rowcount = self._cur.rowcount

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


class _SQLiteConn:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def cursor(self) -> _SQLiteCursor:
        return _SQLiteCursor(self._conn.cursor())

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()


class FakeDB:
    """Fake MySQL db backed by an in-memory SQLite database."""

    def __init__(self):
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._setup()

    def _setup(self):
        # Register MySQL functions that SQLite doesn't have natively
        self._conn.create_function("NOW", 0, lambda: "2000-01-01 00:00:00")
        c = self._conn.cursor()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS cases (
                case_id TEXT PRIMARY KEY,
                group_id TEXT NOT NULL,
                status TEXT NOT NULL,
                problem_title TEXT NOT NULL,
                problem_summary TEXT NOT NULL,
                solution_summary TEXT,
                tags_json TEXT,
                evidence_image_paths_json TEXT,
                in_rag INTEGER NOT NULL DEFAULT 0,
                closed_emoji TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS case_evidence (
                case_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                PRIMARY KEY (case_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS raw_messages (
                message_id TEXT PRIMARY KEY,
                group_id TEXT NOT NULL,
                ts INTEGER NOT NULL,
                sender_hash TEXT NOT NULL,
                sender_name TEXT,
                content_text TEXT,
                image_paths_json TEXT,
                reply_to_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self._conn.commit()

    @contextmanager
    def connection(self):
        yield _SQLiteConn(self._conn)

    # --- inspection helpers -------------------------------------------------

    def fetch_case(self, case_id: str) -> Optional[dict]:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def count_cases(self) -> int:
        cur = self._conn.cursor()
        cur.execute("SELECT count(*) FROM cases")
        return cur.fetchone()[0]

    def count_evidence(self, case_id: str) -> int:
        cur = self._conn.cursor()
        cur.execute("SELECT count(*) FROM case_evidence WHERE case_id = ?", (case_id,))
        return cur.fetchone()[0]

    def insert_raw_message(self, message_id: str, group_id: str, ts: int):
        cur = self._conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO raw_messages(message_id, group_id, ts, sender_hash) VALUES(?,?,?,?)",
            (message_id, group_id, ts, "testhash"),
        )
        self._conn.commit()

    def all_cases(self) -> List[dict]:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM cases ORDER BY created_at")
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _upsert(db: FakeDB, *, case_id="cid1", group_id="g1", status="solved",
            problem_title="GPS not working", problem_summary="GPS fails",
            solution_summary="Turn on tracking", tags=None, evidence_ids=None,
            evidence_image_paths=None):
    from app.db.queries_mysql import upsert_case
    return upsert_case(
        db,
        case_id=case_id,
        group_id=group_id,
        status=status,
        problem_title=problem_title,
        problem_summary=problem_summary,
        solution_summary=solution_summary,
        tags=tags or [],
        evidence_ids=evidence_ids or [],
        evidence_image_paths=evidence_image_paths or [],
    )


# ===========================================================================
# Tests: upsert_case
# ===========================================================================

class TestUpsertCase:

    def test_insert_new_case(self):
        db = FakeDB()
        cid, created = _upsert(db, case_id="abc123")
        assert created is True
        assert cid == "abc123"
        assert db.count_cases() == 1

    def test_same_title_same_group_updates_not_inserts(self):
        """Second upsert with same title+group should UPDATE, not INSERT."""
        db = FakeDB()
        _upsert(db, case_id="first", problem_title="GPS issue", problem_summary="v1")
        assert db.count_cases() == 1

        cid, created = _upsert(db, case_id="second", problem_title="GPS issue", problem_summary="v2")
        # Should still be only one case
        assert db.count_cases() == 1
        assert created is False
        assert cid == "first"  # original id preserved

    def test_override_updates_summary(self):
        """The overriding case should have the new summary content."""
        db = FakeDB()
        _upsert(db, case_id="orig", problem_title="Frozen drone",
                problem_summary="old summary", solution_summary="old solution")

        _upsert(db, case_id="new_id", problem_title="Frozen drone",
                problem_summary="new summary", solution_summary="new solution")

        case = db.fetch_case("orig")
        assert case["problem_summary"] == "new summary"
        assert case["solution_summary"] == "new solution"
        assert case["in_rag"] == 0  # reset to 0 so it gets re-indexed

    def test_override_replaces_evidence(self):
        """Evidence links should be replaced (old ones deleted) on override."""
        db = FakeDB()
        _upsert(db, case_id="orig", problem_title="Frozen drone",
                evidence_ids=["msg1", "msg2"])
        assert db.count_evidence("orig") == 2

        _upsert(db, case_id="ignored", problem_title="Frozen drone",
                evidence_ids=["msg3"])
        assert db.count_evidence("orig") == 1

    def test_different_title_inserts_separate_case(self):
        """Different title → entirely new case, even in the same group."""
        db = FakeDB()
        _upsert(db, case_id="c1", problem_title="GPS issue")
        _upsert(db, case_id="c2", problem_title="Battery issue")
        assert db.count_cases() == 2

    def test_same_title_different_group_inserts_separate(self):
        """Same title but different group → separate cases."""
        db = FakeDB()
        _upsert(db, case_id="c1", group_id="group_A", problem_title="GPS issue")
        _upsert(db, case_id="c2", group_id="group_B", problem_title="GPS issue")
        assert db.count_cases() == 2

    def test_archived_case_not_matched_by_dedup(self):
        """An archived case should be ignored – a new case should be inserted instead."""
        db = FakeDB()
        # Insert a case then manually archive it
        _upsert(db, case_id="old", problem_title="GPS issue", status="solved")
        db._conn.execute("UPDATE cases SET status = 'archived' WHERE case_id = 'old'")
        db._conn.commit()

        cid, created = _upsert(db, case_id="fresh", problem_title="GPS issue", status="solved")
        assert created is True
        assert cid == "fresh"
        assert db.count_cases() == 2

    def test_multiple_duplicates_in_one_run_collapse_to_one(self):
        """Simulates LLM extracting 3 blocks with the same title from overlapping chunks."""
        db = FakeDB()
        results = []
        for i, cid in enumerate(["c1", "c2", "c3"]):
            r = _upsert(db, case_id=cid, problem_title="GPS issue",
                        problem_summary=f"v{i}", solution_summary=f"sol{i}")
            results.append(r)

        assert db.count_cases() == 1
        assert results[0] == ("c1", True)   # first: insert
        assert results[1] == ("c1", False)  # second: update
        assert results[2] == ("c1", False)  # third: update again


# ===========================================================================
# Tests: confirm_cases_by_evidence_ts
# ===========================================================================

class TestConfirmCasesByEvidenceTs:

    def _setup_case_with_evidence(self, db: FakeDB, *,
                                  case_id="c1", group_id="g1", ts=1000,
                                  message_id="msg1", status="solved"):
        """Create a raw_message, case, and case_evidence link."""
        db.insert_raw_message(message_id, group_id, ts)
        _upsert(db, case_id=case_id, group_id=group_id, status=status,
                problem_title="GPS issue", evidence_ids=[message_id])

    def test_positive_emoji_confirms_case(self):
        from app.db.queries_mysql import confirm_cases_by_evidence_ts
        db = FakeDB()
        self._setup_case_with_evidence(db)

        n = confirm_cases_by_evidence_ts(db, group_id="g1", target_ts=1000, emoji="👍")
        assert n == 1

        case = db.fetch_case("c1")
        assert case["closed_emoji"] == "👍"

    def test_returns_zero_if_no_message_at_ts(self):
        from app.db.queries_mysql import confirm_cases_by_evidence_ts
        db = FakeDB()
        self._setup_case_with_evidence(db, ts=1000)

        n = confirm_cases_by_evidence_ts(db, group_id="g1", target_ts=9999, emoji="👍")
        assert n == 0

    def test_returns_zero_if_message_not_evidence(self):
        """Reaction on a message that isn't evidence for any case → no confirmation."""
        from app.db.queries_mysql import confirm_cases_by_evidence_ts
        db = FakeDB()
        db.insert_raw_message("other_msg", "g1", 2000)
        _upsert(db, case_id="c1", group_id="g1", evidence_ids=["msg1"])

        n = confirm_cases_by_evidence_ts(db, group_id="g1", target_ts=2000, emoji="👍")
        assert n == 0

    def test_already_confirmed_case_not_overwritten(self):
        """If closed_emoji is already set, it must not be overwritten by a second reaction."""
        from app.db.queries_mysql import confirm_cases_by_evidence_ts
        db = FakeDB()
        self._setup_case_with_evidence(db, ts=1000)

        confirm_cases_by_evidence_ts(db, group_id="g1", target_ts=1000, emoji="👍")
        n2 = confirm_cases_by_evidence_ts(db, group_id="g1", target_ts=1000, emoji="✅")
        assert n2 == 0  # already confirmed – nothing updated

        case = db.fetch_case("c1")
        assert case["closed_emoji"] == "👍"  # original emoji preserved

    def test_wrong_group_does_not_confirm(self):
        """Reaction in a different group should not match evidence in another group."""
        from app.db.queries_mysql import confirm_cases_by_evidence_ts
        db = FakeDB()
        self._setup_case_with_evidence(db, group_id="g1", ts=1000, message_id="msg1")

        n = confirm_cases_by_evidence_ts(db, group_id="g2", target_ts=1000, emoji="👍")
        assert n == 0

        case = db.fetch_case("c1")
        assert case["closed_emoji"] is None
