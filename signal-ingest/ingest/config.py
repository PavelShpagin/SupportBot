from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, *, default: str | None = None, required: bool = False) -> str:
    val = os.getenv(name, default)
    if required and (val is None or val.strip() == ""):
        raise ValueError(f"Missing required environment variable: {name}")
    return "" if val is None else val


def _env_float(name: str, *, default: float, min_value: float | None = None) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        value = default
    else:
        value = float(raw)
    if min_value is not None and value < min_value:
        raise ValueError(f"{name} must be >= {min_value}, got {value}")
    return value


def _env_int(name: str, *, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


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
    
    # Oracle settings (legacy)
    oracle_user: str
    oracle_password: str
    oracle_dsn: str
    oracle_wallet_dir: str

    openai_api_key: str
    model_blocks: str
    model_img: str

    signal_cli: str
    signal_ingest_storage: str
    history_dir: str
    signal_bot_url: str

    # Signal Desktop settings
    use_signal_desktop: bool
    signal_desktop_url: str

    history_max_seconds: float
    history_idle_seconds: float
    chunk_max_chars: int
    chunk_overlap_messages: int

    worker_poll_seconds: float


def load_settings() -> Settings:
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
        model_blocks=_env("MODEL_BLOCKS", default="gemini-3.1-pro-preview"),
        model_img=_env("MODEL_IMG", default="gemini-3.1-pro-preview"),
        signal_cli=_env("SIGNAL_CLI", default="signal-cli"),
        signal_ingest_storage=_env("SIGNAL_INGEST_STORAGE", default="/var/lib/signal/ingest"),
        history_dir=_env("HISTORY_DIR", default="/var/lib/history"),
        signal_bot_url=_env("SIGNAL_BOT_URL", default="http://signal-bot:8000"),
        # Signal Desktop
        use_signal_desktop=_env_bool("USE_SIGNAL_DESKTOP", default=False),
        signal_desktop_url=_env("SIGNAL_DESKTOP_URL", default="http://signal-desktop-arm64:8001"),
        history_max_seconds=_env_float("HISTORY_MAX_SECONDS", default=180.0, min_value=10.0),
        history_idle_seconds=_env_float("HISTORY_IDLE_SECONDS", default=10.0, min_value=2.0),
        chunk_max_chars=int(_env("HISTORY_CHUNK_MAX_CHARS", default="12000")),
        chunk_overlap_messages=int(_env("HISTORY_CHUNK_OVERLAP_MESSAGES", default="3")),
        worker_poll_seconds=_env_float("WORKER_POLL_SECONDS", default=1.0, min_value=0.1),
    )
