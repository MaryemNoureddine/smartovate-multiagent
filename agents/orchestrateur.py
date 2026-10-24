"""Agent Orchestrateur (hub) : décomposition Chain-of-Thought et consolidation (MAS-TK-06/07)."""
from __future__ import annotations

from autogen_agentchat.agents import AssistantAgent
from autogen_core.model_context import TokenLimitedChatCompletionContext

from agents import prompts
from config.model_client import create_model_client
from config.settings import Settings, get_settings


def create_orchestrateur(settings: Settings | None = None) -> AssistantAgent:
    settings = settings or get_settings()
    client = create_model_client("orchestrateur", settings)
    return AssistantAgent(
        name="orchestrateur",
        description="Planifie la demande en sous-tâches, coordonne l'équipe et consolide la réponse finale (TERMINATE).",
        model_client=client,
        system_message=prompts.ORCHESTRATEUR,
        model_context=TokenLimitedChatCompletionContext(client, token_limit=settings.context_token_limit),
    )
