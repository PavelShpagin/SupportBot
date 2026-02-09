"""
Pytest configuration and fixtures for SupportBot testing.
"""

import json
import os
import sys
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional
from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest

# Add signal-bot app to path
sys.path.insert(0, str(Path(__file__).parent.parent / "signal-bot"))

from app.config import Settings
from app.llm.schemas import CaseResult, DecisionResult, ExtractResult, ImgExtract, RespondResult


# =============================================================================
# Test Settings
# =============================================================================

def make_test_settings(**overrides) -> Settings:
    """Create test settings with reasonable defaults."""
    defaults = {
        "db_backend": "mysql",
        "mysql_host": "localhost",
        "mysql_port": 3306,
        "mysql_user": "test",
        "mysql_password": "test",
        "mysql_database": "supportbot_test",
        "oracle_user": "",
        "oracle_password": "",
        "oracle_dsn": "",
        "oracle_wallet_dir": "",
        "openai_api_key": os.environ.get("GOOGLE_API_KEY", "test-key"),
        "model_img": "gemini-3-pro-preview",
        "model_decision": "gemini-2.5-flash-lite",
        "model_extract": "gemini-2.5-flash-lite",
        "model_case": "gemini-2.5-flash-lite",
        "model_respond": "gemini-3-pro-preview",
        "model_blocks": "gemini-3-pro-preview",
        "embedding_model": "text-embedding-004",
        "chroma_url": "http://localhost:8001",
        "chroma_collection": "test_cases",
        "signal_bot_e164": "+10000000000",
        "signal_bot_storage": "/tmp/signal_test",
        "signal_ingest_storage": "/tmp/signal_ingest_test",
        "signal_cli": "signal-cli",
        "bot_mention_strings": ["@supportbot", "@SupportBot"],
        "signal_listener_enabled": False,
        "log_level": "DEBUG",
        "context_last_n": 40,
        "retrieve_top_k": 5,
        "worker_poll_seconds": 0.1,
        "history_token_ttl_minutes": 60,
        "max_images_per_gate": 3,
        "max_images_per_respond": 5,
        "max_kb_images_per_case": 2,
        "max_image_size_bytes": 5_000_000,
        "max_total_image_bytes": 20_000_000,
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def settings() -> Settings:
    """Provide test settings."""
    return make_test_settings()


# =============================================================================
# In-Memory SQLite Database (for isolated testing)
# =============================================================================

@dataclass
class InMemoryDB:
    """SQLite-based in-memory database for testing (MySQL-compatible API subset)."""
    
    _conn: sqlite3.Connection = field(default=None, repr=False)
    
    def __post_init__(self):
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_schema()
    
    def _create_schema(self):
        """Create tables matching MySQL schema."""
        cur = self._conn.cursor()
        
        cur.execute("""
            CREATE TABLE raw_messages (
                message_id TEXT PRIMARY KEY,
                group_id TEXT NOT NULL,
                ts INTEGER NOT NULL,
                sender_hash TEXT NOT NULL,
                content_text TEXT,
                image_paths_json TEXT,
                reply_to_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cur.execute("""
            CREATE TABLE buffers (
                group_id TEXT PRIMARY KEY,
                buffer_text TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cur.execute("""
            CREATE TABLE cases (
                case_id TEXT PRIMARY KEY,
                group_id TEXT NOT NULL,
                status TEXT NOT NULL,
                problem_title TEXT NOT NULL,
                problem_summary TEXT NOT NULL,
                solution_summary TEXT,
                tags_json TEXT,
                evidence_image_paths_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cur.execute("""
            CREATE TABLE case_evidence (
                case_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                PRIMARY KEY (case_id, message_id)
            )
        """)
        
        cur.execute("""
            CREATE TABLE jobs (
                job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                payload_json TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cur.execute("""
            CREATE TABLE admin_sessions (
                admin_id TEXT PRIMARY KEY,
                pending_group_id TEXT,
                pending_group_name TEXT,
                pending_token TEXT,
                state TEXT NOT NULL DEFAULT 'awaiting_group_name',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cur.execute("""
            CREATE TABLE history_tokens (
                token TEXT PRIMARY KEY,
                admin_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                used_at TIMESTAMP
            )
        """)
        
        self._conn.commit()
    
    def cursor(self):
        return self._conn.cursor()
    
    def commit(self):
        self._conn.commit()
    
    def rollback(self):
        self._conn.rollback()
    
    def connection(self):
        """Context manager for MySQL compatibility."""
        return self
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        pass


@pytest.fixture
def test_db() -> Generator[InMemoryDB, None, None]:
    """Provide an in-memory SQLite database for testing."""
    db = InMemoryDB()
    yield db


# =============================================================================
# Mock LLM Client
# =============================================================================

@dataclass
class MockLLMClient:
    """Mock LLM client for testing without API calls."""
    
    # Configurable responses
    extract_responses: List[ExtractResult] = field(default_factory=list)
    case_responses: List[CaseResult] = field(default_factory=list)
    decision_responses: List[DecisionResult] = field(default_factory=list)
    respond_responses: List[RespondResult] = field(default_factory=list)
    embeddings: Dict[str, List[float]] = field(default_factory=dict)
    
    # Call tracking
    extract_calls: List[str] = field(default_factory=list)
    case_calls: List[str] = field(default_factory=list)
    decision_calls: List[Dict] = field(default_factory=list)
    respond_calls: List[Dict] = field(default_factory=list)
    embed_calls: List[str] = field(default_factory=list)
    
    _extract_idx: int = 0
    _case_idx: int = 0
    _decision_idx: int = 0
    _respond_idx: int = 0
    
    def extract_case_from_buffer(self, *, buffer_text: str) -> ExtractResult:
        self.extract_calls.append(buffer_text)
        if self._extract_idx < len(self.extract_responses):
            result = self.extract_responses[self._extract_idx]
            self._extract_idx += 1
            return result
        # Default: no case found
        return ExtractResult(found=False, case_block="", buffer_new=buffer_text)
    
    def make_case(self, *, case_block_text: str) -> CaseResult:
        self.case_calls.append(case_block_text)
        if self._case_idx < len(self.case_responses):
            result = self.case_responses[self._case_idx]
            self._case_idx += 1
            return result
        # Default: not a valid case
        return CaseResult(
            keep=False, status="open", problem_title="",
            problem_summary="", solution_summary="", tags=[], evidence_ids=[]
        )
    
    def decide_consider(
        self, *, message: str, context: str, images: List[tuple[bytes, str]] | None = None
    ) -> DecisionResult:
        self.decision_calls.append({"message": message, "context": context, "images": images})
        if self._decision_idx < len(self.decision_responses):
            result = self.decision_responses[self._decision_idx]
            self._decision_idx += 1
            return result
        # Default: don't consider
        return DecisionResult(consider=False)
    
    def decide_and_respond(
        self,
        *,
        message: str,
        context: str,
        cases: str,
        images: List[tuple[bytes, str]] | None = None,
    ) -> RespondResult:
        self.respond_calls.append(
            {"message": message, "context": context, "cases": cases, "images": images}
        )
        if self._respond_idx < len(self.respond_responses):
            result = self.respond_responses[self._respond_idx]
            self._respond_idx += 1
            return result
        # Default: don't respond
        return RespondResult(respond=False, text="", citations=[])
    
    def embed(self, *, text: str) -> List[float]:
        self.embed_calls.append(text)
        if text in self.embeddings:
            return self.embeddings[text]
        # Default: return a simple hash-based embedding for testing
        import hashlib
        h = hashlib.sha256(text.encode()).digest()
        return [float(b) / 255.0 for b in h[:128]]  # 128-dim embedding
    
    def image_to_text_json(self, *, image_bytes: bytes, context_text: str) -> ImgExtract:
        return ImgExtract(observations=["test image"], extracted_text="extracted from image")


@pytest.fixture
def mock_llm() -> MockLLMClient:
    """Provide a mock LLM client."""
    return MockLLMClient()


# =============================================================================
# Mock Chroma RAG
# =============================================================================

@dataclass
class MockChromaRag:
    """In-memory mock of ChromaDB for testing."""
    
    cases: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    
    def upsert_case(
        self, *, case_id: str, document: str, embedding: List[float], metadata: Dict[str, Any]
    ) -> None:
        self.cases[case_id] = {
            "case_id": case_id,
            "document": document,
            "embedding": embedding,
            "metadata": metadata,
        }
    
    def retrieve_cases(
        self, *, group_id: str, embedding: List[float], k: int
    ) -> List[Dict[str, Any]]:
        # Filter by group_id and return top-k by simple dot product similarity
        group_cases = [
            c for c in self.cases.values()
            if c["metadata"].get("group_id") == group_id
        ]
        
        def similarity(case):
            case_emb = case["embedding"]
            return sum(a * b for a, b in zip(embedding, case_emb))
        
        sorted_cases = sorted(group_cases, key=similarity, reverse=True)[:k]
        
        return [
            {
                "case_id": c["case_id"],
                "document": c["document"],
                "metadata": c["metadata"],
                "distance": 1.0 - similarity(c),
            }
            for c in sorted_cases
        ]


@pytest.fixture
def mock_rag() -> MockChromaRag:
    """Provide a mock Chroma RAG."""
    return MockChromaRag()


# =============================================================================
# Mock Signal Adapter
# =============================================================================

@dataclass
class MockSignalAdapter:
    """Mock Signal adapter that tracks sent messages."""
    
    sent_messages: List[Dict[str, Any]] = field(default_factory=list)
    
    def send_group_text(self, *, group_id: str, text: str) -> None:
        self.sent_messages.append({
            "type": "group",
            "group_id": group_id,
            "text": text,
        })
    
    def send_direct_text(self, *, recipient: str, text: str) -> None:
        self.sent_messages.append({
            "type": "direct",
            "recipient": recipient,
            "text": text,
        })


@pytest.fixture
def mock_signal() -> MockSignalAdapter:
    """Provide a mock Signal adapter."""
    return MockSignalAdapter()


# =============================================================================
# Test Data: Ukrainian Tech Support Chat
# =============================================================================

# Realistic Ukrainian tech support messages for "Техпідтримка Академія СтабХ"
STABX_SUPPORT_CHAT = [
    # Case 1: Login problem - SOLVED
    {"sender": "user1", "ts": 1707400000000, "text": "Привіт! Не можу зайти в особистий кабінет, пише 'невірний пароль' хоча пароль точно правильний"},
    {"sender": "support1", "ts": 1707400060000, "text": "Вітаю! Спробуйте очистити кеш браузера та cookies. Також перевірте чи не увімкнений Caps Lock"},
    {"sender": "user1", "ts": 1707400120000, "text": "Кеш почистив, не допомогло"},
    {"sender": "support1", "ts": 1707400180000, "text": "Тоді спробуйте скинути пароль через форму відновлення на сторінці входу. Лист прийде на вашу пошту"},
    {"sender": "user1", "ts": 1707400300000, "text": "Скинув пароль, тепер все працює! Дякую!"},
    {"sender": "support1", "ts": 1707400360000, "text": "Радий що допомогло! Якщо будуть питання - звертайтесь"},
    
    # Case 2: Video not playing - SOLVED  
    {"sender": "user2", "ts": 1707401000000, "text": "Добрий день, відео уроки не завантажуються, крутиться колесо і все"},
    {"sender": "support2", "ts": 1707401060000, "text": "Доброго дня! Який браузер використовуєте?"},
    {"sender": "user2", "ts": 1707401120000, "text": "Firefox"},
    {"sender": "support2", "ts": 1707401180000, "text": "Спробуйте в Chrome або Edge. У Firefox іноді бувають проблеми з нашим плеєром"},
    {"sender": "user2", "ts": 1707401300000, "text": "В Chrome запрацювало, дякую!"},
    
    # Case 3: Certificate question - SOLVED
    {"sender": "user3", "ts": 1707402000000, "text": "Скажіть будь ласка, коли можна отримати сертифікат про проходження курсу?"},
    {"sender": "support1", "ts": 1707402060000, "text": "Сертифікат генерується автоматично після завершення всіх модулів та складання фінального тесту з результатом не менше 70%"},
    {"sender": "user3", "ts": 1707402120000, "text": "А де його потім знайти?"},
    {"sender": "support1", "ts": 1707402180000, "text": "В особистому кабінеті -> Мої сертифікати. Там можна завантажити PDF або поділитися посиланням"},
    {"sender": "user3", "ts": 1707402240000, "text": "Зрозуміло, дякую за інформацію!"},
    
    # Case 4: Payment issue - SOLVED
    {"sender": "user4", "ts": 1707403000000, "text": "Оплатив курс але доступ не з'явився, гроші списались з картки"},
    {"sender": "support2", "ts": 1707403060000, "text": "Вкажіть, будь ласка, номер транзакції або email на який оформлювали"},
    {"sender": "user4", "ts": 1707403120000, "text": "Email: user4@gmail.com, транзакція #TRX-2024-8847"},
    {"sender": "support2", "ts": 1707403180000, "text": "Знайшов вашу оплату. Був технічний збій, зараз активую доступ вручну. Зачекайте 5 хвилин і оновіть сторінку"},
    {"sender": "user4", "ts": 1707403300000, "text": "Доступ з'явився, все працює. Дякую за швидку допомогу!"},
    
    # Case 5: Mobile app question - SOLVED
    {"sender": "user5", "ts": 1707404000000, "text": "Чи є мобільний додаток для перегляду курсів?"},
    {"sender": "support1", "ts": 1707404060000, "text": "Так, додаток є для iOS та Android. Шукайте 'СтабХ Академія' в App Store або Google Play"},
    {"sender": "user5", "ts": 1707404120000, "text": "Знайшов, встановив. А чи можна завантажити уроки для офлайн перегляду?"},
    {"sender": "support1", "ts": 1707404180000, "text": "Так, в додатку є кнопка завантаження біля кожного уроку. Завантажені уроки доступні без інтернету протягом 30 днів"},
    {"sender": "user5", "ts": 1707404240000, "text": "Супер, дякую!"},
    
    # Unrelated chatter (should be ignored)
    {"sender": "user1", "ts": 1707405000000, "text": "Привіт всім)"},
    {"sender": "user2", "ts": 1707405060000, "text": "Привіт!"},
    {"sender": "user3", "ts": 1707405120000, "text": "Як справи?"},
    
    # Case 6: Course progress lost - SOLVED
    {"sender": "user6", "ts": 1707406000000, "text": "Допоможіть! Весь мій прогрес по курсу зник, показує що я тільки почав"},
    {"sender": "support2", "ts": 1707406060000, "text": "Це може бути через те що ви зайшли під іншим акаунтом. Перевірте яким email увійшли"},
    {"sender": "user6", "ts": 1707406120000, "text": "Блін, точно! Увійшов через Google а раніше реєструвався через email. Як тепер об'єднати?"},
    {"sender": "support2", "ts": 1707406180000, "text": "Напишіть на support@stabx.academy з обох email адрес з проханням об'єднати акаунти. Ми перенесемо ваш прогрес"},
    {"sender": "user6", "ts": 1707406240000, "text": "Написав на пошту, дякую за підказку!"},
    {"sender": "support2", "ts": 1707406300000, "text": "Перенесли ваш прогрес на основний акаунт. Перевірте будь ласка"},
    {"sender": "user6", "ts": 1707406360000, "text": "Все на місці, дякую величезне!"},
]


@pytest.fixture
def stabx_chat_data() -> List[Dict[str, Any]]:
    """Provide realistic Ukrainian tech support chat data."""
    return STABX_SUPPORT_CHAT.copy()


@pytest.fixture
def stabx_group_id() -> str:
    """Group ID for test data."""
    return "stabx-academy-support-group-123"


# =============================================================================
# Utility Functions
# =============================================================================

def format_chat_buffer(messages: List[Dict[str, Any]]) -> str:
    """Format messages into buffer text like the bot does."""
    lines = []
    for m in messages:
        lines.append(f"{m['sender']} ts={m['ts']}\n{m['text']}\n")
    return "\n".join(lines)


@pytest.fixture
def format_buffer():
    """Provide buffer formatting function."""
    return format_chat_buffer
