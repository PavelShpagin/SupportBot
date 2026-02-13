"""Configuration for Signal Desktop service."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    v = os.environ.get(key)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # Signal Desktop data directory (where SQLite DB lives)
    signal_data_dir: str
    # Signal bot URL to notify about new messages
    signal_bot_url: str
    # How often to poll for new messages (seconds)
    poll_interval_seconds: int
    # Maximum messages to return in a single poll
    max_messages_per_poll: int
    # Port for the HTTP API
    http_port: int


def load_settings() -> Settings:
    return Settings(
        signal_data_dir=_env("SIGNAL_DATA_DIR", "/home/signal/.config/Signal"),
        signal_bot_url=_env("SIGNAL_BOT_URL", "http://signal-bot:8000"),
        poll_interval_seconds=_env_int("POLL_INTERVAL_SECONDS", 5),
        max_messages_per_poll=_env_int("MAX_MESSAGES_PER_POLL", 100),
        http_port=_env_int("HTTP_PORT", 8001),
    )
