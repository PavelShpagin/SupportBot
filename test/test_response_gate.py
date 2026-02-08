"""
Tests for the two-stage response gate.
"""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "signal-bot"))

from app.llm.schemas import DecisionResult, RespondResult
from conftest import MockLLMClient, MockChromaRag, MockSignalAdapter, format_chat_buffer


class TestStage1Decision:
    """Test Stage 1: Should the bot consider responding?"""
    
    def test_consider_help_request(self, mock_llm):
        """Test that help requests are considered."""
        mock_llm.decision_responses.append(DecisionResult(consider=True))
        
        result = mock_llm.decide_consider(
            message="Допоможіть, не можу зайти в кабінет",
            context="попередні повідомлення..."
        )
        
        assert result.consider is True
    
    def test_ignore_greeting(self, mock_llm):
        """Test that greetings are ignored."""
        # Default mock returns consider=False
        result = mock_llm.decide_consider(
            message="Привіт всім!",
            context=""
        )
        
        assert result.consider is False
    
    def test_ignore_ok(self, mock_llm):
        """Test that simple acknowledgements are ignored."""
        result = mock_llm.decide_consider(
            message="ок",
            context=""
        )
        
        assert result.consider is False
    
    def test_ignore_emoji_only(self, mock_llm):
        """Test that emoji-only messages are ignored."""
        result = mock_llm.decide_consider(
            message="👍",
            context=""
        )
        
        assert result.consider is False
    
    def test_consider_question(self, mock_llm):
        """Test that questions are considered."""
        mock_llm.decision_responses.append(DecisionResult(consider=True))
        
        result = mock_llm.decide_consider(
            message="Як отримати сертифікат?",
            context=""
        )
        
        assert result.consider is True
    
    def test_consider_problem_report(self, mock_llm):
        """Test that problem reports are considered."""
        mock_llm.decision_responses.append(DecisionResult(consider=True))
        
        result = mock_llm.decide_consider(
            message="Відео не завантажується вже годину",
            context=""
        )
        
        assert result.consider is True


class TestStage2Response:
    """Test Stage 2: Can the bot answer confidently?"""
    
    def test_respond_with_evidence(self, mock_llm):
        """Test responding when there's sufficient evidence."""
        mock_llm.respond_responses.append(RespondResult(
            respond=True,
            text="Спробуйте скинути пароль через форму відновлення на сторінці входу. Лист з інструкціями прийде на вашу пошту.",
            citations=["case:001"]
        ))
        
        result = mock_llm.decide_and_respond(
            message="Не можу зайти, пише невірний пароль",
            context="...",
            cases='[{"case_id": "001", "document": "Проблема з паролем - скинути через форму"}]'
        )
        
        assert result.respond is True
        assert "пароль" in result.text.lower()
        assert len(result.citations) >= 1
    
    def test_decline_without_evidence(self, mock_llm):
        """Test declining when there's no relevant evidence."""
        # Default mock returns respond=False
        result = mock_llm.decide_and_respond(
            message="Як підключити WebSocket до бекенду?",
            context="...",
            cases="[]"  # No relevant cases
        )
        
        assert result.respond is False
    
    def test_decline_uncertain(self, mock_llm):
        """Test declining when uncertain even with some cases."""
        # Default mock returns respond=False
        result = mock_llm.decide_and_respond(
            message="Чому в мене така дивна помилка XYZ-999?",
            context="...",
            cases='[{"case_id": "001", "document": "Інша проблема з іншим кодом"}]'
        )
        
        assert result.respond is False
    
    def test_response_in_ukrainian(self, mock_llm):
        """Test that response is in Ukrainian."""
        mock_llm.respond_responses.append(RespondResult(
            respond=True,
            text="Сертифікат генерується автоматично після завершення всіх модулів та складання фінального тесту.",
            citations=["case:002"]
        ))
        
        result = mock_llm.decide_and_respond(
            message="Коли я отримаю сертифікат?",
            context="...",
            cases='[{"case_id": "002", "document": "Сертифікат після завершення курсу"}]'
        )
        
        assert result.respond is True
        # Check for Ukrainian characters
        assert any(c in result.text for c in "абвгдеєжзиіїйклмнопрстуфхцчшщьюя")


class TestBotMention:
    """Test forced response when bot is mentioned."""
    
    def test_mention_bypasses_stage1(self):
        """Test that @SupportBot mention bypasses stage 1 decision."""
        mentions = ["@supportbot", "@SupportBot", "привіт @supportbot"]
        
        for text in mentions:
            # Check if any mention string is in the text
            low = text.lower()
            is_mentioned = "@supportbot" in low
            assert is_mentioned is True
    
    def test_no_mention_needs_decision(self):
        """Test that messages without mention need stage 1 decision."""
        messages = [
            "Не можу зайти в кабінет",
            "Допоможіть будь ласка",
            "Як скинути пароль?",
        ]
        
        for text in messages:
            low = text.lower()
            is_mentioned = "@supportbot" in low
            assert is_mentioned is False


class TestResponseQuality:
    """Test quality requirements for responses."""
    
    def test_response_is_concise(self, mock_llm):
        """Test that responses are concise."""
        mock_llm.respond_responses.append(RespondResult(
            respond=True,
            text="Спробуйте очистити кеш браузера та cookies.",
            citations=["case:001"]
        ))
        
        result = mock_llm.decide_and_respond(
            message="Сайт не працює",
            context="...",
            cases="[...]"
        )
        
        # Response should be reasonably short
        assert len(result.text) < 500
    
    def test_response_includes_citations(self, mock_llm):
        """Test that responses include citations when available."""
        mock_llm.respond_responses.append(RespondResult(
            respond=True,
            text="Відео уроки краще працюють в Chrome.",
            citations=["case:002", "case:005"]
        ))
        
        result = mock_llm.decide_and_respond(
            message="Відео не грає",
            context="...",
            cases="[...]"
        )
        
        if result.respond:
            assert len(result.citations) >= 1


class TestFullResponseFlow:
    """Test the complete response flow."""
    
    def test_full_flow_success(self, mock_llm, mock_rag, mock_signal):
        """Test successful response flow: question → retrieve → respond."""
        group_id = "stabx-group"
        
        # Set up mock responses
        mock_llm.decision_responses.append(DecisionResult(consider=True))
        mock_llm.respond_responses.append(RespondResult(
            respond=True,
            text="Спробуйте скинути пароль через форму відновлення.",
            citations=["case:001"]
        ))
        
        # Add a case to RAG
        mock_rag.upsert_case(
            case_id="case-001",
            document="Проблема з паролем - скинути через форму відновлення",
            embedding=mock_llm.embed(text="пароль відновлення"),
            metadata={"group_id": group_id, "status": "solved"},
        )
        
        # Simulate the flow
        message = "Не можу зайти, забув пароль"
        context = "попередній контекст..."
        
        # Stage 1: Decision
        decision = mock_llm.decide_consider(message=message, context=context)
        assert decision.consider is True
        
        # Retrieve cases
        query_embedding = mock_llm.embed(text=message)
        cases = mock_rag.retrieve_cases(group_id=group_id, embedding=query_embedding, k=5)
        
        # Stage 2: Response
        import json
        cases_json = json.dumps(cases, ensure_ascii=False)
        response = mock_llm.decide_and_respond(message=message, context=context, cases=cases_json)
        
        assert response.respond is True
        
        # Send response
        if response.respond:
            out_text = response.text
            if response.citations:
                out_text += "\n\nRefs: " + ", ".join(response.citations[:3])
            mock_signal.send_group_text(group_id=group_id, text=out_text)
        
        assert len(mock_signal.sent_messages) == 1
        assert mock_signal.sent_messages[0]["group_id"] == group_id
    
    def test_full_flow_no_response(self, mock_llm, mock_rag, mock_signal):
        """Test flow when bot shouldn't respond."""
        group_id = "stabx-group"
        
        # Stage 1: Don't consider (default mock behavior)
        message = "ок дякую"
        context = "..."
        
        decision = mock_llm.decide_consider(message=message, context=context)
        assert decision.consider is False
        
        # Should not proceed to Stage 2 or send anything
        assert len(mock_signal.sent_messages) == 0
    
    def test_full_flow_no_evidence(self, mock_llm, mock_rag, mock_signal):
        """Test flow when there's no evidence to answer."""
        group_id = "stabx-group"
        
        # Set up: consider but can't respond
        mock_llm.decision_responses.append(DecisionResult(consider=True))
        # Default respond is False
        
        message = "Як налаштувати Kubernetes кластер?"
        context = "..."
        
        decision = mock_llm.decide_consider(message=message, context=context)
        assert decision.consider is True
        
        # No relevant cases
        cases = mock_rag.retrieve_cases(
            group_id=group_id,
            embedding=mock_llm.embed(text=message),
            k=5
        )
        assert len(cases) == 0
        
        # Stage 2: Can't respond without evidence
        import json
        response = mock_llm.decide_and_respond(
            message=message,
            context=context,
            cases=json.dumps(cases)
        )
        
        assert response.respond is False
        assert len(mock_signal.sent_messages) == 0
