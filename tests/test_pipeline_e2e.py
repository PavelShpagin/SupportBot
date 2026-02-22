"""
End-to-end pipeline test: fixture → LLM extraction → LLM structuring → dedup upsert.

Runs the FULL case-extraction pipeline on the 105-message fixture without Signal,
QR codes, or a real database, then prints the final deduplicated case list.

Requires GOOGLE_API_KEY (same key used in production).
Runtime: ~5–8 min (two LLM passes per chunk × two chunks).

Run:
    GOOGLE_API_KEY=<key> pytest tests/test_pipeline_e2e.py -v -s
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).parent.parent
FIXTURES_DIR = ROOT / "tests" / "fixtures"

# ---------------------------------------------------------------------------
# sys.path setup
# ---------------------------------------------------------------------------

for _d in [str(ROOT / "signal-ingest"), str(ROOT / "signal-bot")]:
    if _d not in sys.path:
        sys.path.insert(0, _d)

for _lib in ("chromadb", "google", "google.generativeai", "mysql", "mysql.connector", "mysql.connector.errors"):
    if _lib not in sys.modules:
        sys.modules[_lib] = MagicMock()


# ---------------------------------------------------------------------------
# Reuse FakeDB from test_db_cases
# ---------------------------------------------------------------------------

import sqlite3
from contextlib import contextmanager


class _SQLiteCursor:
    def __init__(self, cur):
        self._cur = cur
        self.rowcount = 0

    def execute(self, sql, params=()):
        sql = sql.replace("%s", "?")
        if "JOIN case_evidence ce ON ce.case_id = c.case_id" in sql:
            sql = """
            UPDATE cases SET closed_emoji = ?, updated_at = CURRENT_TIMESTAMP
            WHERE case_id IN (SELECT ce.case_id FROM case_evidence ce WHERE ce.message_id = ?)
            AND status IN ('solved','open') AND closed_emoji IS NULL
            """
        self._cur.execute(sql, params)
        self.rowcount = self._cur.rowcount

    def fetchone(self): return self._cur.fetchone()
    def fetchall(self): return self._cur.fetchall()


class _SQLiteConn:
    def __init__(self, conn):
        self._conn = conn
    def cursor(self): return _SQLiteCursor(self._conn.cursor())
    def commit(self): self._conn.commit()
    def rollback(self): self._conn.rollback()


class FakeDB:
    def __init__(self):
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.create_function("NOW", 0, lambda: "2000-01-01 00:00:00")
        c = self._conn.cursor()
        c.executescript("""
            CREATE TABLE cases (
                case_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, status TEXT NOT NULL,
                problem_title TEXT NOT NULL, problem_summary TEXT NOT NULL,
                solution_summary TEXT, tags_json TEXT, evidence_image_paths_json TEXT,
                in_rag INTEGER NOT NULL DEFAULT 0, closed_emoji TEXT, embedding_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE case_evidence (
                case_id TEXT NOT NULL, message_id TEXT NOT NULL,
                PRIMARY KEY (case_id, message_id)
            );
            CREATE TABLE raw_messages (
                message_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, ts INTEGER NOT NULL,
                sender_hash TEXT NOT NULL, sender_name TEXT, content_text TEXT,
                image_paths_json TEXT, reply_to_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self._conn.commit()

    @contextmanager
    def connection(self):
        yield _SQLiteConn(self._conn)

    def all_cases(self):
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM cases ORDER BY created_at")
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_api_key():
    key = os.getenv("GOOGLE_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not key:
        pytest.skip("GOOGLE_API_KEY not set")
    return key


def _make_openai_client(api_key):
    from openai import OpenAI
    return OpenAI(
        api_key=api_key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )


def _make_llm_client(api_key, model):
    """Create a minimal LLMClient without loading full app settings."""
    os.environ.setdefault("GOOGLE_API_KEY", api_key)
    os.environ.setdefault("SIGNAL_BOT_E164", "+10000000000")
    os.environ.setdefault("DB_BACKEND", "mysql")
    os.environ["SIGNAL_LISTENER_ENABLED"] = "false"
    os.environ["USE_SIGNAL_DESKTOP"] = "false"

    from app.llm.client import LLMClient
    settings = MagicMock()
    settings.openai_api_key = api_key
    settings.model_case = model
    settings.embedding_model = os.getenv("EMBEDDING_MODEL", "gemini-embedding-001")
    return LLMClient(settings)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

class TestPipelineE2E:

    def test_full_pipeline_dedup_and_print(self, capsys):
        """
        Full pipeline on the 105-message fixture:
          1. chunk_messages     (pure, instant)
          2. _extract_case_blocks  (LLM call #1: ingest phase)
          3. llm.make_case         (LLM call #2: structuring phase, once per block)
          4. upsert_case           (exact-title dedup into FakeDB)
          5. Print final case list with titles and fake links
        """
        fixture_path = FIXTURES_DIR / "sample_chat.json"
        if not fixture_path.exists():
            pytest.skip("Fixture not found – run fetch_messages.py first")

        api_key = _get_api_key()
        model   = os.getenv("MODEL_BLOCKS", "gemini-3.1-pro-preview")
        data    = json.loads(fixture_path.read_text(encoding="utf-8"))
        messages = data["messages"]
        group_id = data["group_id"]

        from ingest.main import _chunk_messages, _extract_case_blocks
        from app.db.queries_mysql import upsert_case, find_similar_case, merge_case, store_case_embedding

        oc     = _make_openai_client(api_key)
        llm    = _make_llm_client(api_key, model)
        db     = FakeDB()

        # ── Phase 1: extract raw case blocks ──────────────────────────────
        chunks = _chunk_messages(messages=messages, max_chars=12000, overlap_messages=3)
        raw_blocks: List[str] = []
        seen_keys: set = set()
        for chunk in chunks:
            blocks = _extract_case_blocks(openai_client=oc, model=model, chunk_text=chunk)
            for b in blocks:
                key = b[:120]
                if key not in seen_keys:
                    seen_keys.add(key)
                    raw_blocks.append(b)

        # ── Phase 2: structure each block + upsert with dedup ─────────────
        inserted = updated = skipped = 0
        for block in raw_blocks:
            case = llm.make_case(case_block_text=block)
            if not case.keep:
                skipped += 1
                continue

            # Parse evidence msg_ids from block headers
            evidence_ids = list(case.evidence_ids)
            if not evidence_ids:
                for line in block.split("\n"):
                    m = re.search(r"msg_id=(\S+)", line)
                    if m:
                        evidence_ids.append(m.group(1))

            # Semantic dedup: embed problem and find similar existing case
            embed_text = f"{case.problem_title}\n{case.problem_summary}"
            dedup_embedding = llm.embed(text=embed_text)
            similar_id = find_similar_case(db, group_id=group_id, embedding=dedup_embedding)

            if similar_id:
                merge_case(
                    db,
                    target_case_id=similar_id,
                    status=case.status,
                    problem_summary=case.problem_summary,
                    solution_summary=case.solution_summary or "",
                    tags=case.tags,
                    evidence_ids=evidence_ids,
                    evidence_image_paths=[],
                )
                store_case_embedding(db, similar_id, dedup_embedding)
                updated += 1
            else:
                case_id = uuid.uuid4().hex
                final_id, created = upsert_case(
                    db,
                    case_id=case_id,
                    group_id=group_id,
                    status=case.status,
                    problem_title=case.problem_title,
                    problem_summary=case.problem_summary,
                    solution_summary=case.solution_summary or "",
                    tags=case.tags,
                    evidence_ids=evidence_ids,
                    evidence_image_paths=[],
                )
                store_case_embedding(db, final_id, dedup_embedding)
                if created:
                    inserted += 1
                else:
                    updated += 1

        # ── Phase 3: report ───────────────────────────────────────────────
        final_cases = db.all_cases()

        with capsys.disabled():
            print(f"\n{'='*65}")
            print(f"PIPELINE RESULT: {len(messages)} messages → {len(chunks)} chunk(s)")
            print(f"  Raw blocks extracted  : {len(raw_blocks)}")
            print(f"  Skipped (keep=False)  : {skipped}")
            print(f"  Inserted (new)        : {inserted}")
            print(f"  Semantic-merged       : {updated}")
            print(f"  FINAL UNIQUE CASES    : {len(final_cases)}")
            print(f"{'='*65}")
            for i, c in enumerate(final_cases, 1):
                emoji = f" [{c['closed_emoji']}]" if c.get("closed_emoji") else ""
                print(f"  [{i}] [{c['status']}{emoji}] {c['problem_title']}")
                print(f"       https://supportbot.info/case/{c['case_id']}")
            print(f"{'='*65}\n")

        # Assertions
        assert len(raw_blocks) >= 1, "No case blocks extracted from 105 messages"
        assert len(final_cases) >= 1, "No cases survived into DB"
        assert len(final_cases) < len(raw_blocks), (
            f"Dedup had no effect: {len(final_cases)} cases == {len(raw_blocks)} raw blocks."
        )
        # The fixture has ~3 distinct problems; semantic dedup should reduce to ≤ 6
        assert len(final_cases) <= 6, (
            f"Expected ≤6 semantically unique cases from this fixture (3 real problems), "
            f"got {len(final_cases)}. Check threshold or embedding quality."
        )
