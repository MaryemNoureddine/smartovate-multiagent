"""Configuration commune : mode simulation, aucune dépendance cloud, journaux en répertoire temporaire."""
from __future__ import annotations

import os

import pytest

os.environ.update({
    "LLM_MODE": "mock",
    "MOCK_LATENCY_SECONDS": "0",
    "API_KEY": "test-key",
    "REDIS_URL": "",
    "COSMOS_ENDPOINT": "",
    "COSMOS_KEY": "",
    "AZURE_OPENAI_ENDPOINT": "",
    "AZURE_OPENAI_API_KEY": "",
    "HUMAN_IN_THE_LOOP": "false",
    "TEAM_PATTERN": "selector",
    "APPLICATIONINSIGHTS_CONNECTION_STRING": "",
})


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    from config.settings import get_settings
    from core.runner import reset_task_manager
    from memory.cosmos_logger import reset_cosmos_logger
    from memory.redis_memory import reset_session_memory

    get_settings.cache_clear()
    reset_session_memory()
    reset_cosmos_logger()
    reset_task_manager()
    yield
    get_settings.cache_clear()
    reset_session_memory()
    reset_cosmos_logger()
    reset_task_manager()


@pytest.fixture
def settings():
    from config.settings import get_settings

    return get_settings()
