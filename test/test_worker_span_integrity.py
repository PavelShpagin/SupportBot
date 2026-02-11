"""
Integrity tests for span-based extraction and deterministic buffer trimming.
"""

import sys
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent.parent / "signal-bot"))

from app.db import RawMessage
from app.jobs.worker import (
    WorkerDeps,
    _format_buffer_line,
    _format_numbered_buffer_for_extract,
    _handle_buffer_update,
    _parse_buffer_blocks,
)
from app.llm.schemas import CaseResult, ExtractResult, ExtractedCaseSpan


def test_extract_result_rejects_overlapping_spans() -> None:
    with pytest.raises(ValidationError):
        ExtractResult(
            cases=[
                ExtractedCaseSpan(start_idx=0, end_idx=2, start_line=1, end_line=9, case_block="a"),
                ExtractedCaseSpan(start_idx=2, end_idx=3, start_line=10, end_line=15, case_block="b"),
            ]
        )


def test_parse_buffer_blocks_and_numbered_format_are_stable() -> None:
    now = int(time.time() * 1000)
    m1 = RawMessage(
        message_id="m1",
        group_id="g1",
        ts=now - 3000,
        sender_hash="user-a",
        content_text="First message",
        image_paths=[],
        reply_to_id=None,
    )
    m2 = RawMessage(
        message_id="m2",
        group_id="g1",
        ts=now - 2000,
        sender_hash="user-b",
        content_text="Second message",
        image_paths=[],
        reply_to_id=None,
    )
    m3 = RawMessage(
        message_id="m3",
        group_id="g1",
        ts=now - 1000,
        sender_hash="user-c",
        content_text="Third message",
        image_paths=[],
        reply_to_id=None,
    )

    buffer_text = _format_buffer_line(m1) + _format_buffer_line(m2) + _format_buffer_line(m3)
    blocks = _parse_buffer_blocks(buffer_text)

    assert [b.idx for b in blocks] == [0, 1, 2]
    assert [b.raw_text for b in blocks] == [_format_buffer_line(m1), _format_buffer_line(m2), _format_buffer_line(m3)]

    numbered = _format_numbered_buffer_for_extract(blocks)
    assert "### MSG idx=0" in numbered
    assert "### MSG idx=1" in numbered
    assert "### MSG idx=2" in numbered
    assert numbered.count("### END") == 3


def test_handle_buffer_update_removes_only_accepted_span(
    monkeypatch, settings, mock_llm, mock_rag, mock_signal
) -> None:
    now = int(time.time() * 1000)
    group_id = "g-span"
    new_message_id = "msg-new"

    m1 = RawMessage(
        message_id="m1",
        group_id=group_id,
        ts=now - 3000,
        sender_hash="user-1",
        content_text="Problem appears",
        image_paths=[],
        reply_to_id=None,
    )
    m2 = RawMessage(
        message_id="m2",
        group_id=group_id,
        ts=now - 2000,
        sender_hash="support-1",
        content_text="Resolved with fix",
        image_paths=[],
        reply_to_id=None,
    )
    m3 = RawMessage(
        message_id=new_message_id,
        group_id=group_id,
        ts=now - 1000,
        sender_hash="user-2",
        content_text="A new unrelated question",
        image_paths=[],
        reply_to_id=None,
    )

    existing_buffer = _format_buffer_line(m1) + _format_buffer_line(m2)
    expected_remaining = _format_buffer_line(m3)
    captured: dict[str, str] = {}
    inserted_case_ids: list[str] = []

    def fake_get_raw_message(_db, message_id: str):
        if message_id == new_message_id:
            return m3
        return None

    def fake_get_buffer(_db, *, group_id: str) -> str:
        assert group_id == "g-span"
        return existing_buffer

    def fake_set_buffer(_db, *, group_id: str, buffer_text: str) -> None:
        captured[group_id] = buffer_text

    def fake_new_case_id(_db) -> str:
        return "case-1"

    def fake_insert_case(_db, **kwargs) -> None:
        inserted_case_ids.append(kwargs["case_id"])

    monkeypatch.setattr("app.jobs.worker.get_raw_message", fake_get_raw_message)
    monkeypatch.setattr("app.jobs.worker.get_buffer", fake_get_buffer)
    monkeypatch.setattr("app.jobs.worker.set_buffer", fake_set_buffer)
    monkeypatch.setattr("app.jobs.worker.new_case_id", fake_new_case_id)
    monkeypatch.setattr("app.jobs.worker.insert_case", fake_insert_case)

    mock_llm.extract_responses.append(
        ExtractResult(
            cases=[
                ExtractedCaseSpan(
                    start_idx=0,
                    end_idx=1,
                    start_line=1,
                    end_line=6,
                    case_block=_format_buffer_line(m1) + _format_buffer_line(m2),
                )
            ]
        )
    )
    mock_llm.case_responses.append(
        CaseResult(
            keep=True,
            status="solved",
            problem_title="Resolved test issue",
            problem_summary="A reproducible support issue",
            solution_summary="Apply the known fix",
            tags=["test", "span"],
            evidence_ids=[],
        )
    )

    deps = WorkerDeps(settings=settings, db=object(), llm=mock_llm, rag=mock_rag, signal=mock_signal)
    _handle_buffer_update(deps, {"group_id": group_id, "message_id": new_message_id})

    assert captured[group_id] == expected_remaining
    assert inserted_case_ids == ["case-1"]
    assert len(mock_rag.cases) == 1
    assert len(mock_llm.extract_calls) == 1
    assert "### MSG idx=0" in mock_llm.extract_calls[0]
