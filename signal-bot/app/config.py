from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List


def _env(name: str, *, default: str | None = None, required: bool = False) -> str:
    val = os.getenv(name, default)
    if required and (val is None or val.strip() == ""):
        raise ValueError(f"Missing required environment variable: {name}")
    return "" if val is None else val


def _env_int(name: str, *, default: int, min_value: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        value = default
    else:
        value = int(raw)
    if min_value is not None and value < min_value:
        raise ValueError(f"{name} must be >= {min_value}, got {value}")
    return value


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    # Database backend
    db_backend: str  # "mysql" or "oracle"
    
    # MySQL settings
    mysql_host: str
    mysql_port: int
    mysql_user: str
    mysql_password: str
    mysql_database: str
    
    # Oracle DB (legacy, for backwards compatibility)
    oracle_user: str
    oracle_password: str
    oracle_dsn: str
    oracle_wallet_dir: str

    # OpenAI
    openai_api_key: str
    model_img: str
    model_decision: str
    model_extract: str
    model_case: str
    model_respond: str
    model_blocks: str
    embedding_model: str

    # Chroma
    chroma_url: str
    chroma_collection: str

    # Signal
    signal_bot_e164: str
    signal_bot_storage: str
    signal_ingest_storage: str
    signal_cli: str
    bot_mention_strings: List[str]
    signal_listener_enabled: bool

    # Behavior
    log_level: str
    context_last_n: int
    retrieve_top_k: int
    worker_poll_seconds: float
    history_token_ttl_minutes: int


def load_settings() -> Settings:
    mentions = [
        s.strip()
        for s in _env("BOT_MENTION_STRINGS", default="@supportbot").split(",")
        if s.strip()
    ]
    
    db_backend = _env("DB_BACKEND", default="mysql").lower()

    return Settings(
        db_backend=db_backend,
        # MySQL settings (default)
        mysql_host=_env("MYSQL_HOST", default="db"),
        mysql_port=_env_int("MYSQL_PORT", default=3306),
        mysql_user=_env("MYSQL_USER", default="supportbot"),
        mysql_password=_env("MYSQL_PASSWORD", default="supportbot"),
        mysql_database=_env("MYSQL_DATABASE", default="supportbot"),
        # Oracle settings (legacy)
        oracle_user=_env("ORACLE_USER", default=""),
        oracle_password=_env("ORACLE_PASSWORD", default=""),
        oracle_dsn=_env("ORACLE_DSN", default=""),
        oracle_wallet_dir=_env("ORACLE_WALLET_DIR", default=_env("TNS_ADMIN", default="")),
        openai_api_key=_env("GOOGLE_API_KEY", required=True),
        model_img=_env("MODEL_IMG", default="gemini-3-pro-preview"),
        model_decision=_env("MODEL_DECISION", default="gemini-2.5-flash-lite"),
        model_extract=_env("MODEL_EXTRACT", default="gemini-2.5-flash-lite"),
        model_case=_env("MODEL_CASE", default="gemini-2.5-flash-lite"),
        model_respond=_env("MODEL_RESPOND", default="gemini-3-pro-preview"),
        model_blocks=_env("MODEL_BLOCKS", default="gemini-3-pro-preview"),
        embedding_model=_env("EMBEDDING_MODEL", default="text-embedding-004"),
        chroma_url=_env("CHROMA_URL", default="http://rag:8000"),
        chroma_collection=_env("CHROMA_COLLECTION", default="cases"),
        signal_bot_e164=_env("SIGNAL_BOT_E164", required=True),
        signal_bot_storage=_env("SIGNAL_BOT_STORAGE", default="/var/lib/signal/bot"),
        signal_ingest_storage=_env("SIGNAL_INGEST_STORAGE", default="/var/lib/signal/ingest"),
        signal_cli=_env("SIGNAL_CLI", default="signal-cli"),
        bot_mention_strings=mentions,
        signal_listener_enabled=_env_bool("SIGNAL_LISTENER_ENABLED", default=True),
        log_level=_env("LOG_LEVEL", default="INFO"),
        context_last_n=_env_int("CONTEXT_LAST_N", default=40, min_value=1),
        retrieve_top_k=_env_int("RETRIEVE_TOP_K", default=5, min_value=1),
        worker_poll_seconds=float(os.getenv("WORKER_POLL_SECONDS", "1")),
        history_token_ttl_minutes=_env_int("HISTORY_TOKEN_TTL_MINUTES", default=60, min_value=1),
    )

