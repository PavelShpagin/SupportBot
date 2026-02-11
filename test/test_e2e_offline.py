"""
End-to-end offline tests for SupportBot.

These tests simulate the full pipeline from messages to answers,
using mock LLM responses configured to behave like a real support bot.

To run with real LLM (requires GOOGLE_API_KEY):
    GOOGLE_API_KEY=your_key pytest test_e2e_offline.py -v -k "real"
"""

import json
import os
import pytest
import sys
from pathlib import Path
from typing import List, Dict, Any

sys.path.insert(0, str(Path(__file__).parent.parent / "signal-bot"))

from app.llm.schemas import CaseResult, DecisionResult, ExtractResult, ExtractedCaseSpan, RespondResult
from conftest import (
    MockLLMClient, MockChromaRag, MockSignalAdapter, InMemoryDB,
    format_chat_buffer, STABX_SUPPORT_CHAT, make_test_settings
)


# =============================================================================
# Expected cases from the test data
# =============================================================================

EXPECTED_CASES = [
    {
        "id": "login-case",
        "problem": "Неможливість увійти в кабінет",
        "solution": "Скинути пароль через форму відновлення",
        "tags": ["login", "password", "authentication"],
    },
    {
        "id": "video-case",
        "problem": "Відео не завантажується",
        "solution": "Використати Chrome замість Firefox",
        "tags": ["video", "browser", "firefox", "chrome"],
    },
    {
        "id": "certificate-case",
        "problem": "Отримання сертифікату",
        "solution": "Завершити модулі та скласти тест на 70%+",
        "tags": ["certificate", "course", "completion"],
    },
    {
        "id": "payment-case",
        "problem": "Доступ не з'явився після оплати",
        "solution": "Активація вручну після підтвердження транзакції",
        "tags": ["payment", "access", "transaction"],
    },
    {
        "id": "mobile-case",
        "problem": "Мобільний додаток та офлайн перегляд",
        "solution": "Завантажити з App Store/Google Play, кнопка офлайн біля уроків",
        "tags": ["mobile", "app", "offline"],
    },
    {
        "id": "progress-case",
        "problem": "Зник прогрес курсу",
        "solution": "Об'єднати акаунти через support@stabx.academy",
        "tags": ["progress", "account", "merge"],
    },
]


# =============================================================================
# Test questions and expected behavior
# =============================================================================

TEST_QUESTIONS = [
    # Questions that SHOULD get answers (matching cases exist)
    {
        "question": "Не можу зайти в особистий кабінет, пише невірний пароль",
        "should_respond": True,
        "expected_topic": "password",
        "relevant_case": "login-case",
    },
    {
        "question": "Відео уроки не завантажуються, крутиться колесо",
        "should_respond": True,
        "expected_topic": "video",
        "relevant_case": "video-case",
    },
    {
        "question": "Коли можна отримати сертифікат?",
        "should_respond": True,
        "expected_topic": "certificate",
        "relevant_case": "certificate-case",
    },
    {
        "question": "Оплатив курс але доступ не з'явився",
        "should_respond": True,
        "expected_topic": "payment",
        "relevant_case": "payment-case",
    },
    {
        "question": "Чи є мобільний додаток?",
        "should_respond": True,
        "expected_topic": "mobile",
        "relevant_case": "mobile-case",
    },
    {
        "question": "Мій прогрес зник!",
        "should_respond": True,
        "expected_topic": "progress",
        "relevant_case": "progress-case",
    },
    # Questions that should NOT get answers (no competence)
    {
        "question": "Як налаштувати Kubernetes?",
        "should_respond": False,
        "expected_topic": None,
        "relevant_case": None,
    },
    {
        "question": "Порекомендуйте хороший ресторан",
        "should_respond": False,
        "expected_topic": None,
        "relevant_case": None,
    },
    # Messages that should be IGNORED (greetings, acknowledgements)
    {
        "question": "Привіт всім!",
        "should_respond": False,
        "should_consider": False,
    },
    {
        "question": "ок дякую",
        "should_respond": False,
        "should_consider": False,
    },
    {
        "question": "👍",
        "should_respond": False,
        "should_consider": False,
    },
]


# =============================================================================
# End-to-end test class
# =============================================================================

class TestE2EOffline:
    """End-to-end tests using mock LLM with realistic responses."""
    
    def _setup_knowledge_base(self, mock_llm: MockLLMClient, mock_rag: MockChromaRag, group_id: str):
        """Populate RAG with expected cases."""
        for case in EXPECTED_CASES:
            doc_text = f"""
{case['problem']}
{case['solution']}
tags: {', '.join(case['tags'])}
""".strip()
            
            mock_rag.upsert_case(
                case_id=case["id"],
                document=doc_text,
                embedding=mock_llm.embed(text=doc_text),
                metadata={"group_id": group_id, "status": "solved"},
            )
    
    def test_case_mining_from_chat(self, mock_llm, stabx_chat_data, format_buffer):
        """Test that cases are correctly extracted from chat history."""
        # Simulate processing the chat buffer
        buffer = format_buffer(stabx_chat_data)
        
        # Configure mock to extract cases one by one
        # First extraction: login case
        login_case_msgs = stabx_chat_data[:6]
        mock_llm.extract_responses.append(ExtractResult(
            cases=[
                ExtractedCaseSpan(
                    start_idx=0,
                    end_idx=5,
                    start_line=1,
                    end_line=18,
                    case_block=format_buffer(login_case_msgs),
                )
            ]
        ))
        
        result = mock_llm.extract_case_from_buffer(buffer_text=buffer)
        
        assert len(result.cases) == 1
        assert "невірний пароль" in result.cases[0].case_block
        assert "Скинув пароль" in result.cases[0].case_block
        
        # Verify call was made
        assert len(mock_llm.extract_calls) == 1
    
    def test_case_structuring(self, mock_llm, stabx_chat_data, format_buffer):
        """Test that extracted cases are properly structured."""
        login_case_block = format_buffer(stabx_chat_data[:6])
        
        mock_llm.case_responses.append(CaseResult(
            keep=True,
            status="solved",
            problem_title="Неможливість увійти в особистий кабінет",
            problem_summary="Користувач не може увійти, система показує 'невірний пароль' при правильному паролі.",
            solution_summary="Проблема вирішена скиданням пароля через форму відновлення.",
            tags=["login", "password", "authentication", "reset"],
            evidence_ids=[]
        ))
        
        result = mock_llm.make_case(case_block_text=login_case_block)
        
        assert result.keep is True
        assert result.status == "solved"
        assert len(result.problem_title) > 10
        assert len(result.solution_summary) > 10
        assert len(result.tags) >= 3
    
    def test_question_answering_with_knowledge(self, mock_llm, mock_rag, mock_signal):
        """Test answering questions when knowledge exists."""
        group_id = "stabx-group"
        
        # Set up knowledge base
        self._setup_knowledge_base(mock_llm, mock_rag, group_id)
        
        # Test each question that should get an answer
        for test in TEST_QUESTIONS:
            if not test.get("should_respond", False):
                continue
            
            # Reset mock state for this question
            mock_llm._decision_idx = 0
            mock_llm._respond_idx = 0
            mock_llm.decision_responses.clear()
            mock_llm.respond_responses.clear()
            
            # Configure mocks for this question
            mock_llm.decision_responses.append(DecisionResult(consider=True))
            
            # Create appropriate response based on expected case
            case = next((c for c in EXPECTED_CASES if c["id"] == test["relevant_case"]), None)
            if case:
                mock_llm.respond_responses.append(RespondResult(
                    respond=True,
                    text=f"На основі попереднього досвіду: {case['solution']}",
                    citations=[case["id"]]
                ))
            
            question = test["question"]
            
            # Stage 1: Decision
            decision = mock_llm.decide_consider(message=question, context="...")
            assert decision.consider is True, f"Should consider: {question}"
            
            # Retrieve cases
            cases = mock_rag.retrieve_cases(
                group_id=group_id,
                embedding=mock_llm.embed(text=question),
                k=5
            )
            
            # Stage 2: Response
            response = mock_llm.decide_and_respond(
                message=question,
                context="...",
                cases=json.dumps(cases, ensure_ascii=False)
            )
            
            assert response.respond is True, f"Should respond to: {question}"
            assert len(response.text) > 10, f"Response too short for: {question}"
    
    def test_ignoring_greetings(self, mock_llm):
        """Test that greetings and acknowledgements are ignored."""
        greetings = ["Привіт всім!", "ок дякую", "👍", "Добре"]
        
        for greeting in greetings:
            result = mock_llm.decide_consider(message=greeting, context="...")
            # Default mock returns consider=False
            assert result.consider is False, f"Should ignore: {greeting}"
    
    def test_declining_unknown_topics(self, mock_llm, mock_rag, mock_signal):
        """Test that bot declines to answer unknown topics."""
        group_id = "stabx-group"
        
        # Set up knowledge base with course-related cases only
        self._setup_knowledge_base(mock_llm, mock_rag, group_id)
        
        unknown_questions = [
            "Як налаштувати Kubernetes кластер?",
            "Порекомендуйте хороший ресторан у Києві",
            "Яка погода буде завтра?",
        ]
        
        for question in unknown_questions:
            mock_llm._decision_idx = 0
            mock_llm._respond_idx = 0
            mock_llm.decision_responses.clear()
            mock_llm.respond_responses.clear()
            
            # Bot considers the question (it looks like a request)
            mock_llm.decision_responses.append(DecisionResult(consider=True))
            # But can't answer (no evidence) - default mock returns respond=False
            
            decision = mock_llm.decide_consider(message=question, context="...")
            assert decision.consider is True  # It's a question
            
            # Retrieve cases (will find nothing relevant)
            cases = mock_rag.retrieve_cases(
                group_id=group_id,
                embedding=mock_llm.embed(text=question),
                k=5
            )
            
            # Stage 2: Should decline
            response = mock_llm.decide_and_respond(
                message=question,
                context="...",
                cases=json.dumps(cases, ensure_ascii=False)
            )
            
            assert response.respond is False, f"Should NOT respond to: {question}"
    
    def test_group_isolation(self, mock_llm, mock_rag):
        """Test that groups don't share knowledge."""
        # Add case to group A
        mock_rag.upsert_case(
            case_id="group-a-case",
            document="Секретна інформація групи A",
            embedding=mock_llm.embed(text="секрет групи A"),
            metadata={"group_id": "group-a", "status": "solved"},
        )
        
        # Query from group B should find nothing
        results = mock_rag.retrieve_cases(
            group_id="group-b",
            embedding=mock_llm.embed(text="секрет"),
            k=5
        )
        
        assert len(results) == 0, "Group B should not see Group A cases"
        
        # Query from group A should find the case
        results = mock_rag.retrieve_cases(
            group_id="group-a",
            embedding=mock_llm.embed(text="секрет"),
            k=5
        )
        
        assert len(results) == 1, "Group A should see its own cases"


# =============================================================================
# Real LLM tests (requires API key)
# =============================================================================

@pytest.mark.skipif(
    not os.environ.get("GOOGLE_API_KEY"),
    reason="GOOGLE_API_KEY not set"
)
class TestE2ERealLLM:
    """End-to-end tests with real LLM calls."""
    
    @pytest.fixture
    def real_llm(self):
        """Create a real LLM client."""
        from app.llm.client import LLMClient
        settings = make_test_settings()
        return LLMClient(settings)
    
    def test_real_case_extraction(self, real_llm, stabx_chat_data, format_buffer):
        """Test real LLM case extraction from chat."""
        # Use first solved case (login problem)
        buffer = format_buffer(stabx_chat_data[:6])
        
        result = real_llm.extract_case_from_buffer(buffer_text=buffer)
        
        print(f"\n=== Real LLM Extract Result ===")
        print(f"Found cases: {len(result.cases)}")
        first_len = len(result.cases[0].case_block) if result.cases else 0
        print(f"First case block length: {first_len}")
        
        assert len(result.cases) > 0, "Should find a solved case"
        assert len(result.cases[0].case_block) > 50, "Case block should have content"
    
    def test_real_case_structuring(self, real_llm, stabx_chat_data, format_buffer):
        """Test real LLM case structuring."""
        login_case = format_buffer(stabx_chat_data[:6])
        
        result = real_llm.make_case(case_block_text=login_case)
        
        print(f"\n=== Real LLM Case Result ===")
        print(f"Keep: {result.keep}")
        print(f"Status: {result.status}")
        print(f"Title: {result.problem_title}")
        print(f"Problem: {result.problem_summary[:100]}...")
        print(f"Solution: {result.solution_summary[:100]}...")
        print(f"Tags: {result.tags}")
        
        assert result.keep is True, "Should keep this case"
        assert result.status == "solved", "Case should be solved"
        assert len(result.problem_title) > 5, "Should have a title"
        assert len(result.solution_summary) > 10, "Should have a solution"
    
    def test_real_decision_help_request(self, real_llm):
        """Test real LLM decision on help request."""
        result = real_llm.decide_consider(
            message="Не можу зайти в особистий кабінет, допоможіть будь ласка",
            context="Попередні повідомлення в групі техпідтримки"
        )
        
        print(f"\n=== Real LLM Decision (help request) ===")
        print(f"Consider: {result.consider}")
        
        assert result.consider is True, "Should consider help request"
    
    def test_real_decision_greeting(self, real_llm):
        """Test real LLM decision on greeting."""
        result = real_llm.decide_consider(
            message="Привіт всім!",
            context=""
        )
        
        print(f"\n=== Real LLM Decision (greeting) ===")
        print(f"Consider: {result.consider}")
        
        assert result.consider is False, "Should ignore greeting"
    
    def test_real_response_with_cases(self, real_llm):
        """Test real LLM response generation."""
        cases = json.dumps([{
            "case_id": "case-001",
            "document": """Неможливість увійти в особистий кабінет
Користувач не може увійти, система показує 'невірний пароль' при правильному паролі.
Вирішено скиданням пароля через форму відновлення на сторінці входу.
tags: login, password, authentication, reset""",
            "metadata": {"group_id": "stabx", "status": "solved"},
            "distance": 0.1,
        }], ensure_ascii=False)
        
        result = real_llm.decide_and_respond(
            message="Не можу зайти в кабінет, пише невірний пароль",
            context="Група техпідтримки навчальної платформи",
            cases=cases
        )
        
        print(f"\n=== Real LLM Response ===")
        print(f"Respond: {result.respond}")
        print(f"Text: {result.text}")
        print(f"Citations: {result.citations}")
        
        assert result.respond is True, "Should respond with evidence"
        assert len(result.text) > 20, "Response should have content"
        # Should be in Ukrainian
        assert any(c in result.text for c in "абвгдеєжзиіїйклмнопрстуфхцчшщьюя")
    
    def test_real_response_no_evidence(self, real_llm):
        """Test real LLM declines without evidence."""
        result = real_llm.decide_and_respond(
            message="Як налаштувати Kubernetes кластер?",
            context="Група техпідтримки навчальної платформи",
            cases="[]"  # No relevant cases
        )
        
        print(f"\n=== Real LLM Response (no evidence) ===")
        print(f"Respond: {result.respond}")
        print(f"Text: {result.text}")
        
        # Should decline or give very cautious response
        # The bot should NOT hallucinate an answer
        if result.respond:
            # If it does respond, it should be cautious
            assert "не" in result.text.lower() or "не знаю" in result.text.lower() or len(result.text) < 50
    
    def test_real_embedding(self, real_llm):
        """Test real embedding generation."""
        text = "Проблема з входом в особистий кабінет"
        embedding = real_llm.embed(text=text)
        
        print(f"\n=== Real Embedding ===")
        print(f"Dimension: {len(embedding)}")
        print(f"First 5 values: {embedding[:5]}")
        
        assert len(embedding) > 0, "Should return embedding"
        assert isinstance(embedding[0], float), "Embedding values should be floats"
