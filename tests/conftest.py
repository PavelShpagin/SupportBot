"""
Pytest configuration for SupportBot tests.

The test suite imports from three separate services (signal-bot, signal-desktop,
signal-ingest) all of which use top-level package names that may collide (e.g.
both signal-bot and signal-desktop expose an `app` package).

This conftest uses an autouse fixture to swap the active `app` namespace in
sys.modules before each test class runs, preventing import collisions.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Which test classes belong to which service root
# ---------------------------------------------------------------------------
_SIGNAL_BOT_TESTS = {
    "TestFetchMessageAttachments",
    "TestSaveHistoryImages",
}
_SIGNAL_DESKTOP_TESTS = {
    "TestAttachmentEndpoint",
    "TestDbReaderAttachmentParsing",
}
# Tests that manage their own sys.path (signal-ingest); the autouse fixture leaves them alone
_SIGNAL_INGEST_TESTS = {
    "TestChunkMessages",
    "TestCaseExtraction",
    "TestPipelineE2E",
}
# DB-layer tests for signal-bot: need the signal-bot app package but NOT the full startup
_SIGNAL_BOT_DB_TESTS = {
    "TestUpsertCase",
    "TestConfirmCasesByEvidenceTs",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clear_app_modules() -> None:
    """Remove all cached app.* entries from sys.modules."""
    for key in list(sys.modules.keys()):
        if key == "app" or key.startswith("app."):
            del sys.modules[key]


def _prioritize(service_dir: str) -> None:
    """Ensure *service_dir* is the first entry in sys.path that contains app/."""
    path = str(ROOT / service_dir)
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)


# ---------------------------------------------------------------------------
# Pre-stub heavy external libraries that are not installed in the test env
# but are imported transitively when loading service modules.
# ---------------------------------------------------------------------------

def _stub_if_missing(name: str) -> None:
    """Register a MagicMock as *name* in sys.modules if not already present."""
    if name not in sys.modules:
        mock = MagicMock()
        sys.modules[name] = mock
        # Also register common sub-modules that might be imported directly
        for suffix in ("client", "server", "legacy", "asyncio", "caching"):
            sys.modules[f"{name}.{suffix}"] = MagicMock()


for _lib in (
    "chromadb",
    "google",
    "google.generativeai",
    "google.generativeai.caching",
    "mysql",
    "mysql.connector",
    "mysql.connector.errors",
):
    _stub_if_missing(_lib)


# ---------------------------------------------------------------------------
# Autouse fixture – swap app namespace per test class
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_service_namespace(request):
    """
    Before each test, ensure sys.path and sys.modules have the right
    service's 'app' package loaded so imports inside tests resolve correctly.
    """
    class_name = request.node.cls.__name__ if request.node.cls else None

    if class_name in _SIGNAL_INGEST_TESTS:
        # signal-ingest tests manage their own sys.path; nothing to do here
        yield
        return
    elif class_name in _SIGNAL_BOT_DB_TESTS:
        _clear_app_modules()
        _prioritize("signal-bot")
    elif class_name in _SIGNAL_BOT_TESTS:
        _clear_app_modules()
        _prioritize("signal-bot")
        # signal-bot/app/main.py calls load_settings() and creates adapters at import time
        os.environ.setdefault("GOOGLE_API_KEY", "test-fake-key")
        os.environ.setdefault("SIGNAL_BOT_E164", "+10000000000")
        os.environ.setdefault("DB_BACKEND", "mysql")
        # Disable signal-cli and signal-desktop startup checks so import succeeds
        os.environ["SIGNAL_LISTENER_ENABLED"] = "false"
        os.environ["USE_SIGNAL_DESKTOP"] = "false"
    elif class_name in _SIGNAL_DESKTOP_TESTS:
        _clear_app_modules()
        _prioritize("signal-desktop")

    yield

    # Clean up after each test so the next one starts fresh
    _clear_app_modules()
