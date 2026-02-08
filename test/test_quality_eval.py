#!/usr/bin/env python3
"""
Quality evaluation tests for SupportBot using Gemini as judge.

This evaluates:
1. Response quality (helpful, accurate, concise)
2. No hallucinations (only answers based on evidence)
3. No false alerts (ignores greetings, acknowledges)
4. Ukrainian language quality
5. Appropriate silence when no knowledge exists

Run with:
    GOOGLE_API_KEY=your_key python -m pytest test_quality_eval.py -v -s
"""

import json
import os
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "signal-bot"))

# Skip all tests if no API key
pytestmark = pytest.mark.skipif(
    not os.environ.get("GOOGLE_API_KEY"),
    reason="GOOGLE_API_KEY not set"
)


@dataclass
class EvalResult:
    """Result of a quality evaluation."""
    passed: bool
    score: float  # 0-10
    reasoning: str
    details: Dict[str, Any]


# =============================================================================
# Test data: Realistic Ukrainian tech support scenarios
# =============================================================================

# Knowledge base cases (what the bot knows)
KNOWLEDGE_BASE = [
    {
        "case_id": "case-001",
        "problem": "Неможливість увійти в особистий кабінет",
        "problem_detail": "Користувач не може увійти, система показує 'невірний пароль' навіть при правильному паролі.",
        "solution": "Скинути пароль через форму відновлення на сторінці входу. Лист з інструкціями прийде на пошту.",
        "tags": ["login", "password", "authentication"],
    },
    {
        "case_id": "case-002",
        "problem": "Відео уроки не завантажуються",
        "problem_detail": "Відео не грає в браузері Firefox - крутиться індикатор без результату.",
        "solution": "Використати браузер Chrome або Edge. Firefox має проблеми сумісності з відеоплеєром платформи.",
        "tags": ["video", "browser", "firefox", "chrome"],
    },
    {
        "case_id": "case-003",
        "problem": "Отримання сертифікату після курсу",
        "problem_detail": "Користувач хоче знати коли і де отримати сертифікат.",
        "solution": "Сертифікат генерується автоматично після завершення всіх модулів та складання фінального тесту з результатом не менше 70%. Знаходиться в Особистий кабінет → Мої сертифікати.",
        "tags": ["certificate", "course", "completion"],
    },
    {
        "case_id": "case-004",
        "problem": "Немає доступу після оплати курсу",
        "problem_detail": "Гроші списались з картки, але доступ до курсу не з'явився.",
        "solution": "Написати в підтримку з номером транзакції та email. Підтримка активує доступ вручну після підтвердження оплати.",
        "tags": ["payment", "access", "transaction"],
    },
    {
        "case_id": "case-005",
        "problem": "Мобільний додаток та офлайн перегляд",
        "problem_detail": "Чи є мобільний додаток і можливість завантажити уроки.",
        "solution": "Так, додаток 'СтабХ Академія' є в App Store та Google Play. Кнопка завантаження біля кожного уроку дозволяє офлайн перегляд протягом 30 днів.",
        "tags": ["mobile", "app", "offline", "download"],
    },
]

# Test scenarios
TEST_SCENARIOS = [
    # === SHOULD RESPOND WITH GOOD ANSWER ===
    {
        "id": "login_problem",
        "category": "should_answer",
        "question": "Привіт, не можу зайти в кабінет, пише невірний пароль хоча я точно правильно ввожу",
        "context": "Група техпідтримки онлайн-академії",
        "expected_behavior": "Respond with password reset solution",
        "relevant_cases": ["case-001"],
    },
    {
        "id": "video_not_playing",
        "category": "should_answer",
        "question": "Добрий день! Відео уроки взагалі не вантажаться, вже годину чекаю",
        "context": "Група техпідтримки",
        "expected_behavior": "Suggest trying Chrome browser",
        "relevant_cases": ["case-002"],
    },
    {
        "id": "certificate_question",
        "category": "should_answer",
        "question": "Скажіть будь ласка, коли я отримаю сертифікат?",
        "context": "Група техпідтримки курсу",
        "expected_behavior": "Explain certificate requirements (complete modules, 70% test)",
        "relevant_cases": ["case-003"],
    },
    {
        "id": "payment_issue",
        "category": "should_answer",
        "question": "Оплатив курс вчора, гроші списались але доступу немає!",
        "context": "Група техпідтримки",
        "expected_behavior": "Ask for transaction number, explain manual activation",
        "relevant_cases": ["case-004"],
    },
    {
        "id": "mobile_app",
        "category": "should_answer",
        "question": "А є мобільний додаток? Хочу в метро дивитися уроки",
        "context": "Група техпідтримки академії",
        "expected_behavior": "Confirm app exists, mention offline download feature",
        "relevant_cases": ["case-005"],
    },
    
    # === SHOULD STAY SILENT (no knowledge) ===
    {
        "id": "unknown_kubernetes",
        "category": "should_decline",
        "question": "Як налаштувати Kubernetes кластер для продакшену?",
        "context": "Група техпідтримки онлайн-академії",
        "expected_behavior": "Do NOT answer - unrelated topic",
        "relevant_cases": [],
    },
    {
        "id": "unknown_restaurant",
        "category": "should_decline",
        "question": "Порекомендуйте хороший ресторан у Києві",
        "context": "Група техпідтримки",
        "expected_behavior": "Do NOT answer - completely off-topic",
        "relevant_cases": [],
    },
    {
        "id": "unknown_specific_error",
        "category": "should_decline",
        "question": "У мене помилка XYZ-9999 при компіляції модуля, що робити?",
        "context": "Група техпідтримки",
        "expected_behavior": "Do NOT answer - no knowledge about this error",
        "relevant_cases": [],
    },
    
    # === SHOULD IGNORE (greetings, noise) ===
    {
        "id": "greeting_hello",
        "category": "should_ignore",
        "question": "Привіт всім!",
        "context": "Група техпідтримки",
        "expected_behavior": "Ignore - just a greeting",
        "relevant_cases": [],
    },
    {
        "id": "acknowledgement_ok",
        "category": "should_ignore",
        "question": "ок дякую",
        "context": "Попереднє питання про пароль було вирішено",
        "expected_behavior": "Ignore - just acknowledgement",
        "relevant_cases": [],
    },
    {
        "id": "emoji_only",
        "category": "should_ignore",
        "question": "👍",
        "context": "Група техпідтримки",
        "expected_behavior": "Ignore - just emoji",
        "relevant_cases": [],
    },
    {
        "id": "chit_chat",
        "category": "should_ignore",
        "question": "Як справи? Що нового?",
        "context": "Група техпідтримки",
        "expected_behavior": "Ignore - casual chat",
        "relevant_cases": [],
    },
]


# =============================================================================
# Gemini Judge
# =============================================================================

class GeminiJudge:
    """Uses Gemini to evaluate response quality."""
    
    def __init__(self):
        from openai import OpenAI
        self.client = OpenAI(
            api_key=os.environ["GOOGLE_API_KEY"],
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
        self.model = "gemini-2.5-flash-lite"  # Fast, cheap for judging
    
    def evaluate_response(
        self,
        question: str,
        response_text: str,
        expected_behavior: str,
        knowledge_base: List[Dict],
        category: str,
    ) -> EvalResult:
        """Evaluate a bot response."""
        
        kb_text = "\n".join([
            f"Case {c['case_id']}: {c['problem']}\nSolution: {c['solution']}"
            for c in knowledge_base
        ])
        
        prompt = f"""You are evaluating a Ukrainian tech support bot's response.

KNOWLEDGE BASE (what the bot knows):
{kb_text}

USER QUESTION: {question}

BOT RESPONSE: {response_text if response_text else "(NO RESPONSE - bot stayed silent)"}

EXPECTED BEHAVIOR: {expected_behavior}
CATEGORY: {category}

Evaluate the response on these criteria:
1. ACCURACY: Is the information correct based on the knowledge base? (no hallucinations)
2. RELEVANCE: Does it address the user's question?
3. CONCISENESS: Is it appropriately brief without unnecessary fluff?
4. LANGUAGE: Is it in proper Ukrainian?
5. APPROPRIATE_ACTION: 
   - For "should_answer": Did it provide a helpful answer?
   - For "should_decline": Did it correctly NOT answer (stay silent or say "I don't know")?
   - For "should_ignore": Did it correctly stay silent?

Return JSON with:
- passed: boolean (overall pass/fail)
- score: number 0-10
- accuracy_ok: boolean
- relevance_ok: boolean  
- concise_ok: boolean
- language_ok: boolean
- action_ok: boolean
- reasoning: string (1-2 sentences explaining the evaluation)
- issues: array of strings (any problems found)
"""
        
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        
        data = json.loads(resp.choices[0].message.content or "{}")
        
        return EvalResult(
            passed=data.get("passed", False),
            score=data.get("score", 0),
            reasoning=data.get("reasoning", ""),
            details={
                "accuracy_ok": data.get("accuracy_ok"),
                "relevance_ok": data.get("relevance_ok"),
                "concise_ok": data.get("concise_ok"),
                "language_ok": data.get("language_ok"),
                "action_ok": data.get("action_ok"),
                "issues": data.get("issues", []),
            }
        )
    
    def evaluate_no_hallucination(
        self,
        question: str,
        response_text: str,
        knowledge_base: List[Dict],
    ) -> EvalResult:
        """Specifically check for hallucinations."""
        
        kb_text = "\n".join([
            f"Case {c['case_id']}: {c['solution']}"
            for c in knowledge_base
        ])
        
        prompt = f"""Check if this bot response contains HALLUCINATIONS (made-up information not in the knowledge base).

KNOWLEDGE BASE:
{kb_text}

USER QUESTION: {question}

BOT RESPONSE: {response_text}

Return JSON with:
- hallucination_free: boolean (true if NO hallucinations found)
- hallucinations: array of strings (any hallucinated facts)
- reasoning: string
"""
        
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        
        data = json.loads(resp.choices[0].message.content or "{}")
        
        return EvalResult(
            passed=data.get("hallucination_free", False),
            score=10.0 if data.get("hallucination_free") else 0.0,
            reasoning=data.get("reasoning", ""),
            details={"hallucinations": data.get("hallucinations", [])}
        )


# =============================================================================
# Real LLM Client for Testing
# =============================================================================

class RealBotTester:
    """Tests the actual bot LLM pipeline."""
    
    def __init__(self):
        from app.llm.client import LLMClient
        from app.config import Settings
        
        self.settings = Settings(
            db_backend="mysql",
            mysql_host="localhost",
            mysql_port=3306,
            mysql_user="test",
            mysql_password="test",
            mysql_database="test",
            oracle_user="",
            oracle_password="",
            oracle_dsn="",
            oracle_wallet_dir="",
            openai_api_key=os.environ["GOOGLE_API_KEY"],
            model_img="gemini-2.0-flash",
            model_decision="gemini-2.5-flash-lite",
            model_extract="gemini-2.5-flash-lite",
            model_case="gemini-2.5-flash-lite",
            model_respond="gemini-2.0-flash",
            model_blocks="gemini-2.0-flash",
            embedding_model="text-embedding-004",
            chroma_url="http://localhost:8001",
            chroma_collection="test",
            signal_bot_e164="+10000000000",
            signal_bot_storage="/tmp",
            signal_ingest_storage="/tmp",
            signal_cli="signal-cli",
            bot_mention_strings=["@supportbot"],
            signal_listener_enabled=False,
            log_level="INFO",
            context_last_n=40,
            retrieve_top_k=5,
            worker_poll_seconds=1,
            history_token_ttl_minutes=60,
        )
        
        self.llm = LLMClient(self.settings)
    
    def format_cases_for_prompt(self, cases: List[Dict]) -> str:
        """Format cases as JSON for the respond prompt."""
        formatted = []
        for c in cases:
            formatted.append({
                "case_id": c["case_id"],
                "document": f"{c['problem']}\n{c['problem_detail']}\n{c['solution']}\ntags: {', '.join(c['tags'])}",
                "metadata": {"group_id": "test", "status": "solved"},
            })
        return json.dumps(formatted, ensure_ascii=False, indent=2)
    
    def test_decision(self, question: str, context: str) -> bool:
        """Test Stage 1: Should consider?"""
        result = self.llm.decide_consider(message=question, context=context)
        return result.consider
    
    def test_response(self, question: str, context: str, cases: List[Dict]) -> Optional[str]:
        """Test Stage 2: Generate response."""
        cases_json = self.format_cases_for_prompt(cases)
        result = self.llm.decide_and_respond(
            message=question,
            context=context,
            cases=cases_json,
        )
        
        if result.respond:
            text = result.text
            if result.citations:
                text += f"\n\nРеф: {', '.join(result.citations[:3])}"
            return text
        return None


# =============================================================================
# Tests
# =============================================================================

@pytest.fixture(scope="module")
def bot_tester():
    """Create bot tester instance."""
    return RealBotTester()


@pytest.fixture(scope="module")
def judge():
    """Create Gemini judge instance."""
    return GeminiJudge()


class TestResponseQuality:
    """Test response quality with Gemini judge."""
    
    def test_should_answer_scenarios(self, bot_tester, judge):
        """Test scenarios where bot SHOULD provide a helpful answer."""
        
        scenarios = [s for s in TEST_SCENARIOS if s["category"] == "should_answer"]
        results = []
        
        print("\n" + "="*80)
        print("TESTING: Should Answer Scenarios")
        print("="*80)
        
        for scenario in scenarios:
            print(f"\n--- {scenario['id']} ---")
            print(f"Q: {scenario['question']}")
            
            # Get relevant cases from knowledge base
            relevant_cases = [
                c for c in KNOWLEDGE_BASE 
                if c["case_id"] in scenario["relevant_cases"]
            ]
            
            # Test decision
            consider = bot_tester.test_decision(
                scenario["question"],
                scenario["context"]
            )
            print(f"Stage 1 (consider): {consider}")
            
            if not consider:
                print("❌ FAIL: Bot didn't consider this question!")
                results.append({"id": scenario["id"], "passed": False, "reason": "Stage 1 rejected"})
                continue
            
            # Test response
            response = bot_tester.test_response(
                scenario["question"],
                scenario["context"],
                relevant_cases
            )
            print(f"Response: {response}")
            
            if not response:
                print("❌ FAIL: Bot didn't respond!")
                results.append({"id": scenario["id"], "passed": False, "reason": "No response"})
                continue
            
            # Judge the response
            eval_result = judge.evaluate_response(
                question=scenario["question"],
                response_text=response,
                expected_behavior=scenario["expected_behavior"],
                knowledge_base=relevant_cases,
                category=scenario["category"],
            )
            
            status = "✅ PASS" if eval_result.passed else "❌ FAIL"
            print(f"{status} (score: {eval_result.score}/10)")
            print(f"Reasoning: {eval_result.reasoning}")
            if eval_result.details.get("issues"):
                print(f"Issues: {eval_result.details['issues']}")
            
            results.append({
                "id": scenario["id"],
                "passed": eval_result.passed,
                "score": eval_result.score,
                "response": response,
            })
        
        # Summary
        passed = sum(1 for r in results if r["passed"])
        print(f"\n{'='*80}")
        print(f"SHOULD ANSWER: {passed}/{len(results)} passed")
        
        assert passed == len(results), f"Some answer scenarios failed"
    
    def test_should_decline_scenarios(self, bot_tester, judge):
        """Test scenarios where bot should NOT answer (no knowledge)."""
        
        scenarios = [s for s in TEST_SCENARIOS if s["category"] == "should_decline"]
        results = []
        
        print("\n" + "="*80)
        print("TESTING: Should Decline Scenarios (no hallucinations)")
        print("="*80)
        
        for scenario in scenarios:
            print(f"\n--- {scenario['id']} ---")
            print(f"Q: {scenario['question']}")
            
            # Test decision - may or may not consider
            consider = bot_tester.test_decision(
                scenario["question"],
                scenario["context"]
            )
            print(f"Stage 1 (consider): {consider}")
            
            if not consider:
                print("✅ PASS: Bot correctly ignored at Stage 1")
                results.append({"id": scenario["id"], "passed": True, "reason": "Ignored at Stage 1"})
                continue
            
            # Test response with NO relevant cases
            response = bot_tester.test_response(
                scenario["question"],
                scenario["context"],
                []  # No relevant cases!
            )
            
            if response is None:
                print("✅ PASS: Bot correctly declined to respond")
                results.append({"id": scenario["id"], "passed": True, "reason": "Declined"})
            else:
                print(f"Response: {response}")
                # Check if response is actually helpful or just declining
                eval_result = judge.evaluate_response(
                    question=scenario["question"],
                    response_text=response,
                    expected_behavior=scenario["expected_behavior"],
                    knowledge_base=[],
                    category=scenario["category"],
                )
                
                # For "should_decline", passing means NOT answering substantively
                if eval_result.passed:
                    print("✅ PASS: Bot's response was appropriate")
                else:
                    print(f"❌ FAIL: Bot hallucinated an answer!")
                    print(f"Reasoning: {eval_result.reasoning}")
                
                results.append({
                    "id": scenario["id"],
                    "passed": eval_result.passed,
                    "response": response,
                })
        
        # Summary
        passed = sum(1 for r in results if r["passed"])
        print(f"\n{'='*80}")
        print(f"SHOULD DECLINE: {passed}/{len(results)} passed")
        
        assert passed == len(results), f"Bot hallucinated on some scenarios"
    
    def test_should_ignore_scenarios(self, bot_tester):
        """Test scenarios where bot should stay completely silent."""
        
        scenarios = [s for s in TEST_SCENARIOS if s["category"] == "should_ignore"]
        results = []
        
        print("\n" + "="*80)
        print("TESTING: Should Ignore Scenarios (greetings, noise)")
        print("="*80)
        
        for scenario in scenarios:
            print(f"\n--- {scenario['id']} ---")
            print(f"Q: {scenario['question']}")
            
            # Test decision
            consider = bot_tester.test_decision(
                scenario["question"],
                scenario["context"]
            )
            print(f"Stage 1 (consider): {consider}")
            
            if not consider:
                print("✅ PASS: Bot correctly ignored")
                results.append({"id": scenario["id"], "passed": True})
            else:
                print("❌ FAIL: Bot shouldn't consider this!")
                results.append({"id": scenario["id"], "passed": False})
        
        # Summary
        passed = sum(1 for r in results if r["passed"])
        print(f"\n{'='*80}")
        print(f"SHOULD IGNORE: {passed}/{len(results)} passed")
        
        assert passed == len(results), f"Bot responded to noise"


class TestHallucinations:
    """Specific tests for hallucination detection."""
    
    def test_no_hallucination_in_answers(self, bot_tester, judge):
        """Verify bot doesn't make up information."""
        
        print("\n" + "="*80)
        print("TESTING: No Hallucinations")
        print("="*80)
        
        test_cases = [
            {
                "question": "Як скинути пароль?",
                "cases": [KNOWLEDGE_BASE[0]],  # Only login case
            },
            {
                "question": "Відео не працює",
                "cases": [KNOWLEDGE_BASE[1]],  # Only video case
            },
        ]
        
        for tc in test_cases:
            print(f"\nQ: {tc['question']}")
            
            response = bot_tester.test_response(
                tc["question"],
                "Група техпідтримки",
                tc["cases"]
            )
            
            if response:
                print(f"Response: {response}")
                
                eval_result = judge.evaluate_no_hallucination(
                    tc["question"],
                    response,
                    tc["cases"]
                )
                
                if eval_result.passed:
                    print("✅ No hallucinations detected")
                else:
                    print(f"❌ Hallucinations found: {eval_result.details.get('hallucinations')}")
                
                assert eval_result.passed, f"Hallucination detected: {eval_result.details}"


class TestUkrainianQuality:
    """Test Ukrainian language quality."""
    
    def test_responses_in_ukrainian(self, bot_tester):
        """Verify responses are in Ukrainian."""
        
        print("\n" + "="*80)
        print("TESTING: Ukrainian Language")
        print("="*80)
        
        questions = [
            "Не можу зайти в кабінет",
            "Відео не грає",
            "Коли сертифікат?",
        ]
        
        for i, q in enumerate(questions):
            response = bot_tester.test_response(
                q,
                "Група техпідтримки",
                [KNOWLEDGE_BASE[i]]
            )
            
            if response:
                print(f"\nQ: {q}")
                print(f"A: {response}")
                
                # Check for Ukrainian characters
                ukrainian_chars = set("абвгґдеєжзиіїйклмнопрстуфхцчшщьюя")
                has_ukrainian = any(c.lower() in ukrainian_chars for c in response)
                
                if has_ukrainian:
                    print("✅ Contains Ukrainian text")
                else:
                    print("❌ No Ukrainian characters found!")
                
                assert has_ukrainian, f"Response not in Ukrainian: {response}"


class TestConciseness:
    """Test that responses are appropriately concise."""
    
    def test_responses_are_concise(self, bot_tester):
        """Verify responses are not too long."""
        
        print("\n" + "="*80)
        print("TESTING: Conciseness")
        print("="*80)
        
        MAX_LENGTH = 500  # characters
        
        for case in KNOWLEDGE_BASE[:3]:
            response = bot_tester.test_response(
                case["problem"],
                "Група техпідтримки",
                [case]
            )
            
            if response:
                length = len(response)
                status = "✅" if length <= MAX_LENGTH else "⚠️"
                print(f"\n{status} Response length: {length} chars")
                print(f"   {response[:100]}...")
                
                assert length <= MAX_LENGTH * 1.5, f"Response too long: {length} chars"


# =============================================================================
# Run all evaluations
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
