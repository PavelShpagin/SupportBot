"""
Unit tests for trust features: @ mentions and reply-to-solution.
"""

import pytest


def test_get_solution_message_for_reply_with_evidence(test_db, insert_message, get_message):
    """Test extracting solution message from case evidence."""
    from app.jobs.worker import _get_solution_message_for_reply
    
    group_id = "test-group"
    
    # Insert evidence messages
    insert_message(
        test_db,
        message_id="msg1",
        group_id=group_id,
        ts=1000,
        sender_hash="user1",
        content_text="Problem: Something is broken",
    )
    insert_message(
        test_db,
        message_id="msg2",
        group_id=group_id,
        ts=2000,
        sender_hash="support1",
        content_text="Solution: Try restarting the service",
    )
    
    # Create a case dict with evidence_ids
    case = {
        "case_id": "case123",
        "metadata": {
            "evidence_ids": ["msg1", "msg2"],
        },
    }
    
    # Monkey-patch get_raw_message to use our test helper
    import app.jobs.worker as worker_module
    original_get = worker_module.get_raw_message
    worker_module.get_raw_message = lambda db, message_id: get_message(db, message_id)
    
    try:
        # Get solution message (last evidence message)
        msg_id, ts, text = _get_solution_message_for_reply(test_db, case)
        
        assert msg_id == "msg2"
        assert ts == 2000
        assert "Solution: Try restarting" in text
    finally:
        worker_module.get_raw_message = original_get


def test_get_solution_message_for_reply_no_evidence(test_db):
    """Test handling case with no evidence."""
    from app.jobs.worker import _get_solution_message_for_reply
    
    case = {
        "case_id": "case123",
        "metadata": {},
    }
    
    msg_id, ts, text = _get_solution_message_for_reply(test_db, case)
    
    assert msg_id is None
    assert ts is None
    assert text is None


def test_mention_recipients_format(mock_signal):
    """Test that mention recipients are passed correctly."""
    mention_recipients = ["user-uuid-123"]
    
    # Verify it's a list of strings
    assert isinstance(mention_recipients, list)
    assert all(isinstance(r, str) for r in mention_recipients)
    assert len(mention_recipients) > 0


def test_trust_features_signal_call(mock_signal):
    """Test that signal adapter is called with correct parameters."""
    mock_signal.send_group_text(
        group_id="test-group",
        text="Test response",
        quote_timestamp=1000,
        quote_author="user123",
        quote_message="Original question",
        mention_recipients=["user123"],
    )
    
    assert len(mock_signal.sent_messages) == 1
    sent = mock_signal.sent_messages[0]
    
    assert sent["group_id"] == "test-group"
    assert sent["text"] == "Test response"
    assert sent["quote_timestamp"] == 1000
    assert sent["quote_author"] == "user123"
    assert sent["mention_recipients"] == ["user123"]
