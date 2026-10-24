"""Fabrique de clients LLM (Azure OpenAI ou simulation).

- Mode ``azure`` : ``AzureOpenAIChatCompletionClient`` avec version de modèle
  explicite (incident R5 du rapport), ``max_tokens`` strict et température basse.
- Mode ``mock`` : client simulé déterministe, pour développer et tester sans
  coût ni credentials (voir ``config/mock_client.py``).
"""
from __future__ import annotations

import logging

from autogen_core.models import ChatCompletionClient, ModelFamily, ModelInfo

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

GPT4O_MODEL_INFO = ModelInfo(
    vision=True,
    function_calling=True,
    json_output=True,
    structured_output=True,
    family=ModelFamily.GPT_4O,
)


def create_model_client(role: str = "generic", settings: Settings | None = None) -> ChatCompletionClient:
    """Crée un client de complétion pour un rôle d'agent donné."""
    settings = settings or get_settings()
    if settings.effective_llm_mode == "mock":
        from config.mock_client import MockChatCompletionClient

        return MockChatCompletionClient(role=role)

    if not settings.azure_configured:
        raise RuntimeError(
            "LLM_MODE=azure mais AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY ne sont pas renseignés dans le .env"
        )

    from autogen_ext.models.openai import AzureOpenAIChatCompletionClient

    model = settings.azure_openai_model
    if settings.azure_openai_model_version:
        model = f"{model}-{settings.azure_openai_model_version}"

    return AzureOpenAIChatCompletionClient(
        azure_deployment=settings.azure_openai_deployment,
        model=model,
        api_version=settings.azure_openai_api_version,
        azure_endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key.get_secret_value(),
        temperature=settings.temperature,
        max_tokens=settings.max_tokens_per_call,
        model_info=GPT4O_MODEL_INFO,
    )
