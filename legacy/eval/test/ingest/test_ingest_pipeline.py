"""
Unit tests for the history ingestion pipeline.

Covers the things that have been breaking in production:
  1. _chunk_messages — reactions field formatting
  2. P_BLOCKS_SYSTEM — not corrupted, contains required instructions
  3. delete_all_group_data — uses correct table name (chat_groups, not group_docs)
  4. DB schema migrations — required columns and tables are defined
  5. SignalMessage — has reactions field, /group/messages API exposes it

Run with:
  cd /path/to/SupportBot
  python -m pytest test/ingest/ -v
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Paths
ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "signal-ingest"))
sys.path.insert(0, str(ROOT / "signal-bot"))
sys.path.insert(0, str(ROOT / "signal-desktop"))

# Mock heavy dependencies that are only available inside Docker containers,
# not on the host. Must be done before any import from ingest.main.
for _mod in ("openai", "openai.OpenAI"):
    sys.modules.setdefault(_mod, MagicMock())


# =============================================================================
# _chunk_messages
# =============================================================================

class TestChunkMessages:
    """Tests for ingest._chunk_messages."""

    def _chunk(self, messages, max_chars=10000, overlap=0):
        from ingest.main import _chunk_messages
        return _chunk_messages(messages=messages, max_chars=max_chars, overlap_messages=overlap)

    def _msg(self, text, ts=1000, sender="userA", msg_id=None, reactions=0):
        return {
            "body": text,
            "timestamp": ts,
            "sender": sender,
            "id": msg_id or str(ts),
            "reactions": reactions,
        }

    def test_basic_fields_present(self):
        chunks = self._chunk([self._msg("Hello", ts=1001, msg_id="m1")])
        assert "msg_id=m1" in chunks[0]
        assert "Hello" in chunks[0]
        assert "ts=1001" in chunks[0]

    def test_reactions_included_when_nonzero(self):
        chunks = self._chunk([self._msg("Fix worked", ts=1001, msg_id="m1", reactions=3)])
        assert "reactions=3" in chunks[0]

    def test_reactions_omitted_when_zero(self):
        chunks = self._chunk([self._msg("No reaction", ts=1001, msg_id="m1", reactions=0)])
        assert "reactions=" not in chunks[0]

    def test_reactions_omitted_when_absent(self):
        msg = {"body": "text", "timestamp": 1001, "sender": "u", "id": "m1"}
        chunks = self._chunk([msg])
        assert "reactions=" not in chunks[0]

    def test_reactions_coerced_from_string(self):
        chunks = self._chunk([self._msg("test", ts=1001, msg_id="m1", reactions="2")])
        assert "reactions=2" in chunks[0]

    def test_empty_body_skipped(self):
        msgs = [
            self._msg("", ts=1001, msg_id="empty"),
            self._msg("Real message", ts=1002, msg_id="real"),
        ]
        chunks = self._chunk(msgs)
        assert "empty" not in chunks[0]
        assert "Real message" in chunks[0]

    def test_fallback_id_is_timestamp(self):
        msg = {"body": "test", "timestamp": 9999, "sender": "u"}
        chunks = self._chunk([msg])
        assert "msg_id=9999" in chunks[0]

    def test_single_chunk_when_small(self):
        msgs = [self._msg(f"msg {i}", ts=i, msg_id=f"m{i}") for i in range(5)]
        chunks = self._chunk(msgs, max_chars=10000)
        assert len(chunks) == 1

    def test_splits_into_multiple_chunks(self):
        msgs = [self._msg("A" * 80, ts=i, msg_id=f"m{i}") for i in range(20)]
        chunks = self._chunk(msgs, max_chars=400)
        assert len(chunks) > 1

    def test_no_messages_lost_across_chunks(self):
        msgs = [self._msg(f"msg {i}", ts=i, msg_id=f"id{i}") for i in range(10)]
        chunks = self._chunk(msgs, max_chars=200)
        combined = "".join(chunks)
        for i in range(10):
            assert f"msg_id=id{i}" in combined

    def test_overlap_repeats_tail_of_previous_chunk(self):
        msgs = [self._msg(f"msg {i}", ts=i, msg_id=f"m{i}") for i in range(10)]
        chunks_overlap = self._chunk(msgs, max_chars=200, overlap=2)
        chunks_none = self._chunk(msgs, max_chars=200, overlap=0)
        if len(chunks_overlap) > 1:
            total_overlap = sum(len(c) for c in chunks_overlap)
            total_none = sum(len(c) for c in chunks_none)
            assert total_overlap >= total_none


# =============================================================================
# P_BLOCKS_SYSTEM prompt sanity
# =============================================================================

class TestQrTimeout:
    """QR scan window must be long enough for a user to actually scan it."""

    def test_qr_timeout_at_least_5_minutes(self):
        source = (ROOT / "signal-ingest/ingest/main.py").read_text(encoding="utf-8")
        import re
        m = re.search(r"max_wait_seconds\s*=\s*(\d+)", source)
        assert m, "max_wait_seconds not found in ingest/main.py"
        timeout = int(m.group(1))
        assert timeout >= 300, (
            f"QR scan timeout is only {timeout}s ({timeout//60} min). "
            "Should be at least 300s (5 min) to give users enough time."
        )

    def test_qr_timeout_message_matches_actual_timeout(self):
        """The message shown to the user must match the actual timeout."""
        ingest_source = (ROOT / "signal-ingest/ingest/main.py").read_text(encoding="utf-8")
        bot_source = (ROOT / "signal-bot/app/main.py").read_text(encoding="utf-8")
        import re
        m = re.search(r"max_wait_seconds\s*=\s*(\d+)", ingest_source)
        assert m
        timeout_min = int(m.group(1)) // 60
        # Find the waiting message in the bot
        msg_match = re.search(r'Waiting for scan \((\d+) min\)', bot_source)
        assert msg_match, "Could not find 'Waiting for scan (N min)' in signal-bot/app/main.py"
        msg_min = int(msg_match.group(1))
        assert msg_min == timeout_min, (
            f"Bot tells user '{msg_min} min' but actual timeout is {timeout_min} min"
        )


class TestBlocksPrompt:
    """P_BLOCKS_SYSTEM must not be corrupted and must contain valid instructions."""

    def test_prompt_is_not_empty(self):
        from ingest.main import P_BLOCKS_SYSTEM
        assert len(P_BLOCKS_SYSTEM.strip()) > 100

    def test_prompt_not_all_question_marks(self):
        from ingest.main import P_BLOCKS_SYSTEM
        ratio = P_BLOCKS_SYSTEM.count("?") / max(len(P_BLOCKS_SYSTEM), 1)
        assert ratio < 0.3, (
            f"Prompt appears corrupted: {ratio:.0%} question marks. "
            f"First 80 chars: {P_BLOCKS_SYSTEM[:80]!r}"
        )

    def test_prompt_has_json_instruction(self):
        from ingest.main import P_BLOCKS_SYSTEM
        assert "json" in P_BLOCKS_SYSTEM.lower()

    def test_prompt_has_case_block_instruction(self):
        from ingest.main import P_BLOCKS_SYSTEM
        assert "case_block" in P_BLOCKS_SYSTEM.lower()

    def test_prompt_has_solved_instruction(self):
        from ingest.main import P_BLOCKS_SYSTEM
        assert "solved" in P_BLOCKS_SYSTEM.lower()

    def test_prompt_has_reactions_instruction(self):
        from ingest.main import P_BLOCKS_SYSTEM
        assert "reactions" in P_BLOCKS_SYSTEM.lower()

    def test_prompt_has_ukrainian_confirmations(self):
        from ingest.main import P_BLOCKS_SYSTEM
        # Ukrainian "дякую" = \u0434\u044f\u043a\u0443\u044e
        # Ukrainian "працює" = \u043f\u0440\u0430\u0446\u044e\u0454
        uk_words = [
            "\u0434\u044f\u043a\u0443\u044e",   # дякую
            "\u043f\u0440\u0430\u0446\u044e\u0454",  # працює
            "\u0432\u0438\u0440\u0456\u0448\u0435\u043d\u043e",  # вирішено
        ]
        assert any(w in P_BLOCKS_SYSTEM for w in uk_words), (
            "Prompt should include Ukrainian confirmation words (as unicode escapes)"
        )


# =============================================================================
# delete_all_group_data — correct table names
# =============================================================================

class TestDeleteAllGroupData:
    """delete_all_group_data must reference the real table names."""

    def _get_func_body(self):
        source = ROOT / "signal-bot/app/db/queries_mysql.py"
        text = source.read_text(encoding="utf-8")
        start = text.find("def delete_all_group_data")
        assert start != -1, "delete_all_group_data not found"
        end = text.find("\ndef ", start + 1)
        return text[start:end]

    def test_does_not_delete_from_group_docs(self):
        body = self._get_func_body()
        # The stats dict key may still say "group_docs" (harmless label), but
        # there must be no SQL DELETE targeting the non-existent group_docs table.
        assert "DELETE FROM group_docs" not in body, (
            "delete_all_group_data issues DELETE FROM group_docs which doesn't exist. "
            "Use 'chat_groups' instead."
        )

    def test_references_chat_groups(self):
        body = self._get_func_body()
        assert "chat_groups" in body, (
            "delete_all_group_data must DELETE from 'chat_groups'"
        )

    def test_all_deleted_tables_exist_in_schema(self):
        schema_text = (ROOT / "signal-bot/app/db/schema_mysql.py").read_text(encoding="utf-8")
        body = self._get_func_body()
        deleted_tables = re.findall(r"DELETE FROM (\w+)", body)
        assert deleted_tables, "No DELETE FROM statements found"
        for table in deleted_tables:
            assert f"CREATE TABLE {table}" in schema_text, (
                f"Table '{table}' is deleted in delete_all_group_data "
                f"but has no CREATE TABLE in schema_mysql.py"
            )


# =============================================================================
# DB schema migrations
# =============================================================================

class TestSchemaMigrations:
    """Required columns and tables must be in the schema."""

    def _schema(self):
        return (ROOT / "signal-bot/app/db/schema_mysql.py").read_text(encoding="utf-8")

    def test_image_paths_json_in_ddl(self):
        assert "image_paths_json" in self._schema()

    def test_image_paths_json_migration_exists(self):
        assert "ADD COLUMN IF NOT EXISTS image_paths_json" in self._schema()

    def test_evidence_image_paths_json_migration_exists(self):
        assert "ADD COLUMN IF NOT EXISTS evidence_image_paths_json" in self._schema()

    def test_chat_groups_table_defined(self):
        assert "CREATE TABLE chat_groups" in self._schema()

    def test_no_group_docs_table_defined(self):
        assert "CREATE TABLE group_docs" not in self._schema(), (
            "Found CREATE TABLE group_docs — it was renamed to chat_groups"
        )

    def test_reactions_table_defined(self):
        assert "CREATE TABLE reactions" in self._schema()

    def test_jobs_table_defined(self):
        assert "CREATE TABLE jobs" in self._schema()


# =============================================================================
# SignalMessage dataclass — reactions field
# =============================================================================

class TestSignalMessageReactions:
    """SignalMessage must carry a reactions field and the API must expose it."""

    def test_has_reactions_field_defaulting_to_zero(self):
        from app.db_reader import SignalMessage
        msg = SignalMessage(
            id="1", conversation_id="c1", timestamp=1000,
            sender="u1", body="hello", type="incoming"
        )
        assert hasattr(msg, "reactions")
        assert msg.reactions == 0

    def test_reactions_field_accepts_nonzero(self):
        from app.db_reader import SignalMessage
        msg = SignalMessage(
            id="1", conversation_id="c1", timestamp=1000,
            sender="u1", body="hello", type="incoming", reactions=7
        )
        assert msg.reactions == 7

    def test_group_messages_api_includes_reactions_key(self):
        source = (ROOT / "signal-desktop/app/main.py").read_text(encoding="utf-8")
        # Find the /group/messages endpoint return dict
        start = source.find('@app.get("/group/messages")')
        end = source.find("\n@app.", start + 1)
        body = source[start:end] if end != -1 else source[start:]
        assert '"reactions"' in body, (
            "/group/messages response must include 'reactions' field for LLM resolution detection"
        )

    def test_chunk_messages_uses_reactions_from_api_response(self):
        """Verify the field name 'reactions' matches what _chunk_messages reads."""
        from ingest.main import _chunk_messages
        msg = {"body": "test", "timestamp": 1, "sender": "u", "id": "m1", "reactions": 4}
        chunks = _chunk_messages(messages=[msg], max_chars=10000, overlap_messages=0)
        assert "reactions=4" in chunks[0], (
            "_chunk_messages must read the 'reactions' key from API response"
        )
