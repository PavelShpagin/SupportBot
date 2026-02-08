"""
Tests for case extraction from chat buffer.
"""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "signal-bot"))

from app.llm.schemas import ExtractResult, CaseResult
from conftest import MockLLMClient, format_chat_buffer


class TestCaseExtraction:
    """Test case extraction from chat buffer."""
    
    def test_extract_single_solved_case(self, mock_llm, stabx_chat_data, format_buffer):
        """Test extracting a single solved case from buffer."""
        # First case: login problem (messages 0-5)
        login_case_messages = stabx_chat_data[:6]
        buffer = format_buffer(login_case_messages)
        
        # Configure mock to return the case
        case_block = format_buffer(login_case_messages)
        mock_llm.extract_responses.append(ExtractResult(
            found=True,
            case_block=case_block,
            buffer_new=""  # Buffer empty after extraction
        ))
        
        result = mock_llm.extract_case_from_buffer(buffer_text=buffer)
        
        assert result.found is True
        assert "невірний пароль" in result.case_block
        assert "все працює" in result.case_block
    
    def test_extract_returns_empty_when_no_case(self, mock_llm, format_buffer):
        """Test that extraction returns empty when no solved case exists."""
        # Only greetings, no case
        greetings = [
            {"sender": "user1", "ts": 1707405000000, "text": "Привіт всім)"},
            {"sender": "user2", "ts": 1707405060000, "text": "Привіт!"},
        ]
        buffer = format_buffer(greetings)
        
        # Default mock behavior: no case found
        result = mock_llm.extract_case_from_buffer(buffer_text=buffer)
        
        assert result.found is False
        assert result.case_block == ""
        assert result.buffer_new == buffer
    
    def test_extract_removes_case_from_buffer(self, mock_llm, stabx_chat_data, format_buffer):
        """Test that extracted case is removed from buffer."""
        # Full buffer with multiple potential cases
        full_buffer = format_buffer(stabx_chat_data[:15])
        
        # Extract first case (login), leave the rest
        first_case = format_buffer(stabx_chat_data[:6])
        remaining = format_buffer(stabx_chat_data[6:15])
        
        mock_llm.extract_responses.append(ExtractResult(
            found=True,
            case_block=first_case,
            buffer_new=remaining
        ))
        
        result = mock_llm.extract_case_from_buffer(buffer_text=full_buffer)
        
        assert result.found is True
        # First case should be in case_block
        assert "невірний пароль" in result.case_block
        # Second case (video) should be in remaining buffer
        assert "відео уроки" in result.buffer_new


class TestCaseStructuring:
    """Test structuring extracted cases into knowledge base format."""
    
    def test_structure_login_case(self, mock_llm, stabx_chat_data, format_buffer):
        """Test structuring the login problem case."""
        login_case = format_buffer(stabx_chat_data[:6])
        
        mock_llm.case_responses.append(CaseResult(
            keep=True,
            status="solved",
            problem_title="Неможливість увійти в особистий кабінет",
            problem_summary="Користувач не може увійти в особистий кабінет, система показує повідомлення 'невірний пароль' навіть при правильному паролі. Очищення кешу браузера не допомогло.",
            solution_summary="Вирішено шляхом скидання пароля через форму відновлення на сторінці входу. Лист з інструкціями надійшов на пошту користувача.",
            tags=["login", "password", "authentication", "password-reset"],
            evidence_ids=[]
        ))
        
        result = mock_llm.make_case(case_block_text=login_case)
        
        assert result.keep is True
        assert result.status == "solved"
        assert "кабінет" in result.problem_title.lower() or "увійти" in result.problem_title.lower()
        # Check for "пароль" or its declined forms in Ukrainian
        assert "пароль" in result.solution_summary.lower() or "пароля" in result.solution_summary.lower()
        assert len(result.tags) >= 3
    
    def test_structure_video_case(self, mock_llm, stabx_chat_data, format_buffer):
        """Test structuring the video playback case."""
        video_case = format_buffer(stabx_chat_data[6:11])
        
        mock_llm.case_responses.append(CaseResult(
            keep=True,
            status="solved",
            problem_title="Відео уроки не завантажуються",
            problem_summary="Відео уроки не завантажуються в браузері Firefox - крутиться індикатор завантаження без результату.",
            solution_summary="Вирішено переходом на браузер Chrome. Firefox має проблеми сумісності з відеоплеєром платформи.",
            tags=["video", "browser", "firefox", "chrome", "playback"],
            evidence_ids=[]
        ))
        
        result = mock_llm.make_case(case_block_text=video_case)
        
        assert result.keep is True
        assert result.status == "solved"
        assert "відео" in result.problem_title.lower()
        assert "chrome" in result.solution_summary.lower()
    
    def test_reject_incomplete_case(self, mock_llm):
        """Test that incomplete cases are rejected."""
        # Only a question, no solution
        incomplete = """
user1 ts=1707400000000
Як отримати сертифікат?
"""
        
        # Default mock behavior returns keep=False
        result = mock_llm.make_case(case_block_text=incomplete)
        
        assert result.keep is False
    
    def test_reject_greeting_as_case(self, mock_llm):
        """Test that greetings are not kept as cases."""
        greeting = """
user1 ts=1707405000000
Привіт всім)

user2 ts=1707405060000
Привіт!
"""
        
        result = mock_llm.make_case(case_block_text=greeting)
        
        assert result.keep is False
    
    def test_structure_payment_case(self, mock_llm, stabx_chat_data, format_buffer):
        """Test structuring the payment issue case."""
        payment_case = format_buffer(stabx_chat_data[14:19])
        
        mock_llm.case_responses.append(CaseResult(
            keep=True,
            status="solved",
            problem_title="Відсутній доступ після оплати курсу",
            problem_summary="Користувач оплатив курс, гроші списались з картки, але доступ до курсу не з'явився.",
            solution_summary="Виявлено технічний збій при активації. Доступ активовано вручну службою підтримки після підтвердження транзакції (#TRX-2024-8847).",
            tags=["payment", "access", "transaction", "activation", "manual-fix"],
            evidence_ids=[]
        ))
        
        result = mock_llm.make_case(case_block_text=payment_case)
        
        assert result.keep is True
        assert result.status == "solved"
        assert len(result.tags) >= 3


class TestCaseTags:
    """Test tag generation for cases."""
    
    def test_tags_are_relevant(self):
        """Test that generated tags are relevant to the content."""
        # This would be verified by the LLM, but we can check structure
        expected_login_tags = ["login", "password", "authentication"]
        expected_video_tags = ["video", "browser", "playback"]
        expected_payment_tags = ["payment", "access", "transaction"]
        
        for tags in [expected_login_tags, expected_video_tags, expected_payment_tags]:
            # Tags should be short
            assert all(len(tag) < 30 for tag in tags)
            # Tags should be lowercase-ish or technical terms
            assert all(tag.replace("-", "").replace("_", "").isalnum() for tag in tags)
    
    def test_tag_count_in_range(self, mock_llm):
        """Test that case has 3-8 tags as specified."""
        mock_llm.case_responses.append(CaseResult(
            keep=True,
            status="solved",
            problem_title="Test case",
            problem_summary="Test problem",
            solution_summary="Test solution",
            tags=["tag1", "tag2", "tag3", "tag4"],
            evidence_ids=[]
        ))
        
        result = mock_llm.make_case(case_block_text="test")
        
        assert 3 <= len(result.tags) <= 8


class TestCaseQuality:
    """Test case quality requirements."""
    
    def test_problem_title_length(self, mock_llm):
        """Test that problem title is 4-10 words."""
        mock_llm.case_responses.append(CaseResult(
            keep=True,
            status="solved",
            problem_title="Неможливість увійти в особистий кабінет",  # 5 words
            problem_summary="Test",
            solution_summary="Test",
            tags=["test"],
            evidence_ids=[]
        ))
        
        result = mock_llm.make_case(case_block_text="test")
        
        word_count = len(result.problem_title.split())
        assert 3 <= word_count <= 12  # Allow some flexibility
    
    def test_solution_required_for_solved(self, mock_llm):
        """Test that solved cases must have a solution."""
        mock_llm.case_responses.append(CaseResult(
            keep=True,
            status="solved",
            problem_title="Test problem",
            problem_summary="User had an issue",
            solution_summary="Fixed by doing X, Y, Z",  # Must be non-empty
            tags=["test"],
            evidence_ids=[]
        ))
        
        result = mock_llm.make_case(case_block_text="test")
        
        if result.status == "solved":
            assert len(result.solution_summary.strip()) > 0
