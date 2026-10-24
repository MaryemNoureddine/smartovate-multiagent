"""Agent Critique : validation qualité finale et filtrage PII (MAS-TK-12/13)."""
from __future__ import annotations

from autogen_agentchat.agents import AssistantAgent
from autogen_core.model_context import TokenLimitedChatCompletionContext

from agents import prompts
from config.model_client import create_model_client
from config.settings import Settings, get_settings
from tools.base_tools import detect_pii


def create_critique(settings: Settings | None = None) -> AssistantAgent:
    settings = settings or get_settings()
    client = create_model_client("critique", settings)
    return AssistantAgent(
        name="critique",
        description="Contrôle qualité final : vérifie l'adéquation à la demande et l'absence de données personnelles.",
        model_client=client,
        system_message=prompts.CRITIQUE,
        tools=[detect_pii],
        reflect_on_tool_use=True,
        model_context=TokenLimitedChatCompletionContext(client, token_limit=settings.context_token_limit),
    )
