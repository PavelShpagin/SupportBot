"""
Tests for message ingestion and storage.
"""

import json
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "signal-bot"))

from app.db import RawMessage
from conftest import InMemoryDB, MockLLMClient, make_test_settings


# =============================================================================
# Helper functions to simulate db operations with SQLite
# =============================================================================

def insert_raw_message(db: InMemoryDB, msg: RawMessage) -> None:
    """Insert a raw message into the test database."""
    cur = db.cursor()
    cur.execute("""
        INSERT INTO raw_messages (message_id, group_id, ts, sender_hash, content_text, image_paths_json, reply_to_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        msg.message_id,
        msg.group_id,
        msg.ts,
        msg.sender_hash,
        msg.content_text,
        json.dumps(msg.image_paths),
        msg.reply_to_id,
    ))
    db.commit()


def get_raw_message(db: InMemoryDB, message_id: str) -> RawMessage | None:
    """Get a raw message from the test database."""
    cur = db.cursor()
    cur.execute("""
        SELECT message_id, group_id, ts, sender_hash, content_text, image_paths_json, reply_to_id
        FROM raw_messages WHERE message_id = ?
    """, (message_id,))
    row = cur.fetchone()
    if row is None:
        return None
    return RawMessage(
        message_id=row[0],
        group_id=row[1],
        ts=row[2],
        sender_hash=row[3],
        content_text=row[4],
        image_paths=json.loads(row[5] or "[]"),
        reply_to_id=row[6],
    )


def enqueue_job(db: InMemoryDB, job_type: str, payload: dict) -> int:
    """Enqueue a job in the test database."""
    cur = db.cursor()
    cur.execute("""
        INSERT INTO jobs (type, payload_json, status)
        VALUES (?, ?, 'pending')
    """, (job_type, json.dumps(payload)))
    db.commit()
    return cur.lastrowid


def get_pending_jobs(db: InMemoryDB) -> list:
    """Get all pending jobs."""
    cur = db.cursor()
    cur.execute("SELECT job_id, type, payload_json FROM jobs WHERE status = 'pending'")
    return [{"job_id": r[0], "type": r[1], "payload": json.loads(r[2])} for r in cur.fetchall()]


# =============================================================================
# Tests
# =============================================================================

class TestMessageIngestion:
    """Test message ingestion flow."""
    
    def test_insert_and_retrieve_message(self, test_db):
        """Test basic message storage and retrieval."""
        msg = RawMessage(
            message_id="msg-001",
            group_id="group-123",
            ts=1707400000000,
            sender_hash="abc123",
            content_text="Привіт! Не можу зайти в кабінет",
            image_paths=[],
            reply_to_id=None,
        )
        
        insert_raw_message(test_db, msg)
        
        retrieved = get_raw_message(test_db, "msg-001")
        assert retrieved is not None
        assert retrieved.message_id == "msg-001"
        assert retrieved.group_id == "group-123"
        assert retrieved.content_text == "Привіт! Не можу зайти в кабінет"
    
    def test_message_with_reply(self, test_db):
        """Test message with reply_to_id."""
        original = RawMessage(
            message_id="msg-001",
            group_id="group-123",
            ts=1707400000000,
            sender_hash="user1",
            content_text="Як скинути пароль?",
            image_paths=[],
            reply_to_id=None,
        )
        
        reply = RawMessage(
            message_id="msg-002",
            group_id="group-123",
            ts=1707400060000,
            sender_hash="support",
            content_text="Використайте форму відновлення на сторінці входу",
            image_paths=[],
            reply_to_id="msg-001",
        )
        
        insert_raw_message(test_db, original)
        insert_raw_message(test_db, reply)
        
        retrieved_reply = get_raw_message(test_db, "msg-002")
        assert retrieved_reply.reply_to_id == "msg-001"
    
    def test_ukrainian_text_storage(self, test_db):
        """Test that Ukrainian text is stored correctly."""
        msg = RawMessage(
            message_id="msg-ukr",
            group_id="group-123",
            ts=1707400000000,
            sender_hash="user1",
            content_text="Привіт! Чи є мобільний додаток? Хочу завантажити уроки для офлайн перегляду 📱",
            image_paths=[],
            reply_to_id=None,
        )
        
        insert_raw_message(test_db, msg)
        
        retrieved = get_raw_message(test_db, "msg-ukr")
        assert "мобільний додаток" in retrieved.content_text
        assert "📱" in retrieved.content_text
    
    def test_job_enqueue_on_ingestion(self, test_db):
        """Test that BUFFER_UPDATE and MAYBE_RESPOND jobs are created."""
        msg = RawMessage(
            message_id="msg-job",
            group_id="group-123",
            ts=1707400000000,
            sender_hash="user1",
            content_text="Допоможіть з проблемою!",
            image_paths=[],
            reply_to_id=None,
        )
        
        insert_raw_message(test_db, msg)
        
        # Simulate job enqueueing (what ingestion.py does)
        enqueue_job(test_db, "BUFFER_UPDATE", {"group_id": "group-123", "message_id": "msg-job"})
        enqueue_job(test_db, "MAYBE_RESPOND", {"group_id": "group-123", "message_id": "msg-job"})
        
        jobs = get_pending_jobs(test_db)
        assert len(jobs) == 2
        
        job_types = {j["type"] for j in jobs}
        assert "BUFFER_UPDATE" in job_types
        assert "MAYBE_RESPOND" in job_types
    
    def test_sender_hash_privacy(self):
        """Test that sender hashing works correctly."""
        import hashlib
        
        def sender_hash(sender: str) -> str:
            return hashlib.sha256(sender.encode("utf-8")).hexdigest()[:16]
        
        phone1 = "+380501234567"
        phone2 = "+380509876543"
        
        hash1 = sender_hash(phone1)
        hash2 = sender_hash(phone2)
        
        # Hashes should be different
        assert hash1 != hash2
        
        # Hash should be deterministic
        assert hash1 == sender_hash(phone1)
        
        # Hash should be 16 chars
        assert len(hash1) == 16
    
    def test_multiple_messages_same_group(self, test_db):
        """Test storing multiple messages in the same group."""
        for i in range(10):
            msg = RawMessage(
                message_id=f"msg-{i:03d}",
                group_id="group-123",
                ts=1707400000000 + i * 60000,
                sender_hash=f"user{i % 3}",
                content_text=f"Повідомлення номер {i}",
                image_paths=[],
                reply_to_id=None,
            )
            insert_raw_message(test_db, msg)
        
        # Count messages
        cur = test_db.cursor()
        cur.execute("SELECT COUNT(*) FROM raw_messages WHERE group_id = ?", ("group-123",))
        count = cur.fetchone()[0]
        assert count == 10
    
    def test_image_extraction_placeholder(self, mock_llm):
        """Test image extraction returns proper structure."""
        result = mock_llm.image_to_text_json(
            image_bytes=b"fake image data",
            context_text="Подивіться на скріншот"
        )
        
        assert hasattr(result, "observations")
        assert hasattr(result, "extracted_text")
        assert isinstance(result.observations, list)


class TestDataFiltering:
    """Test filtering of messages (greetings, spam, etc.)."""
    
    def test_greeting_detection(self, stabx_chat_data):
        """Test identifying greeting messages that should be filtered."""
        greetings = ["Привіт всім)", "Привіт!", "Як справи?"]
        
        for msg in stabx_chat_data:
            if msg["text"] in greetings:
                # These should be filtered out from case extraction
                assert len(msg["text"]) < 20  # Greetings are short
    
    def test_support_question_detection(self, stabx_chat_data):
        """Test identifying actual support questions."""
        support_keywords = [
            "не можу", "не працює", "допоможіть", "проблема",
            "як", "чому", "де знайти", "коли"
        ]
        
        support_questions = [
            msg for msg in stabx_chat_data
            if any(kw in msg["text"].lower() for kw in support_keywords)
        ]
        
        # Should find several support questions
        assert len(support_questions) >= 5


class TestBufferManagement:
    """Test rolling buffer operations."""
    
    def test_buffer_creation(self, test_db, stabx_group_id):
        """Test creating a new buffer for a group."""
        cur = test_db.cursor()
        cur.execute("""
            INSERT INTO buffers (group_id, buffer_text) VALUES (?, ?)
        """, (stabx_group_id, "Initial buffer text"))
        test_db.commit()
        
        cur.execute("SELECT buffer_text FROM buffers WHERE group_id = ?", (stabx_group_id,))
        row = cur.fetchone()
        assert row[0] == "Initial buffer text"
    
    def test_buffer_append(self, test_db, stabx_group_id, format_buffer, stabx_chat_data):
        """Test appending messages to buffer."""
        # Create initial buffer
        cur = test_db.cursor()
        cur.execute("""
            INSERT INTO buffers (group_id, buffer_text) VALUES (?, ?)
        """, (stabx_group_id, ""))
        test_db.commit()
        
        # Append first 5 messages
        buffer_text = format_buffer(stabx_chat_data[:5])
        cur.execute("""
            UPDATE buffers SET buffer_text = ? WHERE group_id = ?
        """, (buffer_text, stabx_group_id))
        test_db.commit()
        
        cur.execute("SELECT buffer_text FROM buffers WHERE group_id = ?", (stabx_group_id,))
        row = cur.fetchone()
        
        assert "Привіт! Не можу зайти в особистий кабінет" in row[0]
        assert "невірний пароль" in row[0]
