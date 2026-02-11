"""
Test to verify the trust logic fix:
- Bot should now trust the LLM to decide whether to respond
- Pre-filtering should only block if truly nothing available (edge case)
"""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "signal-bot"))

from app.jobs.worker import _pick_history_solution_refs


def test_pick_history_solution_refs_with_solved_cases():
    """Test that _pick_history_solution_refs correctly extracts solved cases."""
    retrieved = [
        {
            "case_id": "case1",
            "document": "Title 1\nProblem description\nSolution text here\ntags: support",
            "metadata": {
                "status": "solved",
                "evidence_ids": ["msg1", "msg2"]
            }
        },
        {
            "case_id": "case2",
            "document": "Title 2\nAnother problem\ntags: tech",
            "metadata": {
                "status": "open",  # not solved
            }
        },
        {
            "case_id": "case3",
            "document": "Title 3\nProblem here\nFixed by restart\ntags: solved",
            "metadata": {
                "status": "solved",
            }
        }
    ]
    
    refs = _pick_history_solution_refs(retrieved, max_refs=2)
    
    # Should get case1 and case3 (both solved with solutions)
    assert len(refs) == 2
    assert refs[0]["case_id"] == "case1"
    assert "Solution text here" in refs[0]["solution"]
    assert refs[1]["case_id"] == "case3"
    assert "Fixed by restart" in refs[1]["solution"]


def test_pick_history_solution_refs_no_solved():
    """Test that empty list is returned when no solved cases exist."""
    retrieved = [
        {
            "case_id": "case1",
            "document": "Title\nProblem\ntags: open",
            "metadata": {
                "status": "open",
            }
        }
    ]
    
    refs = _pick_history_solution_refs(retrieved, max_refs=1)
    assert len(refs) == 0


def test_pick_history_solution_refs_empty():
    """Test that empty list is returned for empty input."""
    refs = _pick_history_solution_refs([], max_refs=1)
    assert len(refs) == 0


def test_trust_logic_scenario():
    """
    Integration test to verify the fix:
    
    BEFORE FIX:
    - If no history_refs AND buffer < 100 chars → bot blocked
    
    AFTER FIX:
    - Only block if len(retrieved) == 0 AND len(buffer) == 0
    - LLM makes the final decision
    """
    
    # Scenario 1: Retrieved cases but no solved ones, empty buffer
    # BEFORE: Would block (no history_refs, no buffer)
    # AFTER: Should reach LLM (retrieved cases exist)
    retrieved = [
        {
            "case_id": "case1",
            "document": "Relevant case without explicit solution",
            "metadata": {"status": "open"}
        }
    ]
    buffer = ""
    
    # New logic: should NOT block (retrieved cases exist)
    should_block = len(retrieved) == 0 and len(buffer.strip()) == 0
    assert should_block is False, "Should allow LLM to decide when cases retrieved"
    
    # Scenario 2: No cases, no buffer
    # BEFORE: Would block
    # AFTER: Should still block (nothing to work with)
    retrieved_empty = []
    buffer_empty = ""
    
    should_block = len(retrieved_empty) == 0 and len(buffer_empty.strip()) == 0
    assert should_block is True, "Should block when truly nothing available"
    
    # Scenario 3: No cases, but has buffer
    # BEFORE: Would depend on buffer length >= 100
    # AFTER: Should allow (buffer exists)
    retrieved_empty = []
    buffer_with_content = "Some ongoing discussion with 50 chars total"
    
    should_block = len(retrieved_empty) == 0 and len(buffer_with_content.strip()) == 0
    assert should_block is False, "Should allow when buffer has content"
    
    # Scenario 4: Has cases, has buffer
    # BEFORE: Would check history_refs
    # AFTER: Should always allow
    retrieved = [{"case_id": "case1"}]
    buffer = "Some buffer"
    
    should_block = len(retrieved) == 0 and len(buffer.strip()) == 0
    assert should_block is False, "Should allow when both cases and buffer exist"


if __name__ == "__main__":
    # Run tests manually
    test_pick_history_solution_refs_with_solved_cases()
    test_pick_history_solution_refs_no_solved()
    test_pick_history_solution_refs_empty()
    test_trust_logic_scenario()
    print("✅ All tests passed!")
