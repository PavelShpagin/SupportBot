"""
Unit tests for the case-extraction pipeline using a real 105-message fixture.

TestChunkMessages: pure unit tests, no API key needed (~0.4 s).
TestCaseExtraction: calls Gemini API, requires GOOGLE_API_KEY (~3 min for 2 chunks).

Run all:
    GOOGLE_API_KEY=<key> pytest tests/test_case_extraction.py -v -s

Run only pure tests (instant, no API):
    pytest tests/test_case_extraction.py::TestChunkMessages -v

Refresh fixture from VM (run once when chat data changes):
    python3 fetch_messages.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).parent.parent
FIXTURES_DIR = ROOT / "tests" / "fixtures"

# ---------------------------------------------------------------------------
# sys.path: make signal-ingest importable as the `ingest` package
# ---------------------------------------------------------------------------

_INGEST_DIR = str(ROOT / "signal-ingest")
if _INGEST_DIR not in sys.path:
    sys.path.insert(0, _INGEST_DIR)

# Stub heavy deps that signal-ingest pulls in transitively
for _lib in ("mysql", "mysql.connector", "mysql.connector.errors"):
    if _lib not in sys.modules:
        sys.modules[_lib] = MagicMock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_fixture() -> dict:
    path = FIXTURES_DIR / "sample_chat.json"
    if not path.exists():
        pytest.skip(f"Fixture not found: {path}. Run fetch_messages.py first.")
    return json.loads(path.read_text(encoding="utf-8"))


def _make_openai_client():
    """Return a real OpenAI-compatible client pointed at Gemini."""
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        pytest.skip("GOOGLE_API_KEY not set – skipping LLM test")
    from openai import OpenAI
    return OpenAI(
        api_key=api_key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestChunkMessages:
    """Pure unit tests – no LLM, no API key needed."""

    def _chunk(self, messages, max_chars=12000, overlap=3):
        from ingest.main import _chunk_messages
        return _chunk_messages(messages=messages, max_chars=max_chars, overlap_messages=overlap)

    def test_105_messages_produce_few_chunks_at_default_size(self):
        data = _load_fixture()
        chunks = self._chunk(data["messages"])
        # 105 messages should produce very few chunks at 12 000-char limit
        assert 1 <= len(chunks) <= 4, f"Expected 1-4 chunks, got {len(chunks)}"

    def test_small_max_chars_produces_multiple_chunks(self):
        data = _load_fixture()
        chunks = self._chunk(data["messages"], max_chars=1500)
        assert len(chunks) > 1

    def test_overlap_messages_appear_in_consecutive_chunks(self):
        data = _load_fixture()
        chunks = self._chunk(data["messages"], max_chars=1500, overlap=3)
        if len(chunks) < 2:
            pytest.skip("Only one chunk produced – increase message count or lower max_chars")
        # Last 3 formatted lines of chunk[0] should appear at the start of chunk[1]
        lines_0 = [l for l in chunks[0].splitlines() if l.strip()]
        lines_1 = [l for l in chunks[1].splitlines() if l.strip()]
        # The last sender-header line of chunk[0] should appear somewhere in chunk[1]
        last_header = next((l for l in reversed(lines_0) if l.startswith("fa") or "ts=" in l), None)
        if last_header:
            assert any(last_header in l for l in lines_1), (
                "Overlap: last message from chunk[0] not found in chunk[1]"
            )

    def test_empty_body_messages_are_skipped(self):
        messages = [
            {"id": "1", "sender": "abc", "ts": 1000, "body": "", "reactions": 0},
            {"id": "2", "sender": "abc", "ts": 2000, "body": "hello", "reactions": 0},
        ]
        chunks = self._chunk(messages)
        assert len(chunks) == 1
        assert "hello" in chunks[0]
        assert "ts=1000" not in chunks[0]

    def test_reactions_appear_in_header(self):
        messages = [
            {"id": "1", "sender": "abc", "ts": 1000, "body": "Fixed the issue", "reactions": 3},
        ]
        chunks = self._chunk(messages)
        assert "reactions=3" in chunks[0]

    def test_no_messages_returns_empty(self):
        chunks = self._chunk([])
        assert chunks == []


class TestCaseExtraction:
    """Integration tests that call the real Gemini API."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = _make_openai_client()
        self.data = _load_fixture()
        from ingest.main import _chunk_messages, _extract_case_blocks
        self._chunk_messages = _chunk_messages
        self._extract_case_blocks = _extract_case_blocks
        settings = MagicMock()
        settings.model_blocks = os.getenv("MODEL_BLOCKS", "gemini-3.1-pro-preview")
        self.settings = settings

    def _run_extraction(self, max_chars=12000, overlap=3) -> List[str]:
        chunks = self._chunk_messages(
            messages=self.data["messages"],
            max_chars=max_chars,
            overlap_messages=overlap,
        )
        all_blocks: List[str] = []
        seen: set[str] = set()
        for chunk in chunks:
            blocks = self._extract_case_blocks(
                openai_client=self.client,
                model=self.settings.model_blocks,
                chunk_text=chunk,
            )
            for b in blocks:
                key = b[:120]
                if key not in seen:
                    seen.add(key)
                    all_blocks.append(b)
        return all_blocks

    def test_extraction_returns_some_cases(self):
        """Smoke test: a reasonable number of solved cases should be found in 105 messages.
        
        The fixture contains a real drone-support chat (~24 resolvable exchanges).
        """
        blocks = self._run_extraction()
        assert 5 <= len(blocks) <= 40, (
            f"Expected 5-40 cases from 105 messages, got {len(blocks)}. "
            f"Check the fixture or the LLM prompt."
        )

    def test_no_excessive_duplicates_with_small_chunks(self):
        """With chunk overlap, duplicate blocks should be deduplicated by the seen-set."""
        blocks_single = self._run_extraction(max_chars=12000, overlap=3)
        blocks_multi = self._run_extraction(max_chars=2000, overlap=3)
        # Deduplicated counts should be close; multi-chunk should not wildly exceed single-chunk
        ratio = len(blocks_multi) / max(len(blocks_single), 1)
        assert ratio <= 3.0, (
            f"Multi-chunk extraction ({len(blocks_multi)}) is >3x single-chunk "
            f"({len(blocks_single)}); deduplication may be broken"
        )

    def test_case_blocks_contain_msg_id_headers(self):
        """Each case block should preserve the original message headers with msg_id."""
        blocks = self._run_extraction()
        for block in blocks:
            assert "msg_id=" in block, (
                f"Case block is missing msg_id headers – evidence linking will fail:\n{block[:300]}"
            )

    def test_extraction_count_stable_across_runs(self):
        """Run extraction twice; counts should be identical (temperature=0)."""
        blocks_a = self._run_extraction()
        blocks_b = self._run_extraction()
        assert len(blocks_a) == len(blocks_b), (
            f"Non-deterministic extraction: run1={len(blocks_a)}, run2={len(blocks_b)}"
        )

    def test_print_extraction_summary(self, capsys):
        """Not a real assertion – prints a human-readable summary for manual inspection."""
        blocks = self._run_extraction()
        with capsys.disabled():
            print(f"\n{'='*60}")
            print(f"EXTRACTION SUMMARY: {len(blocks)} case(s) from {len(self.data['messages'])} messages")
            print(f"{'='*60}")
            for i, block in enumerate(blocks, 1):
                first_line = block.splitlines()[0] if block.splitlines() else ""
                print(f"  [{i}] {first_line[:100]}")
            print(f"{'='*60}\n")
        assert True  # always passes; just for output
