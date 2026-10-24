"""Agents Spécialistes : Codeur et Réviseur (MAS-TK-09/10/11)."""
from __future__ import annotations

from autogen_agentchat.agents import AssistantAgent
from autogen_core.model_context import TokenLimitedChatCompletionContext

from agents import prompts
from config.model_client import create_model_client
from config.settings import Settings, get_settings
from tools.base_tools import calculate, check_code_safety, run_python_snippet


def create_codeur(settings: Settings | None = None, name: str = "codeur") -> AssistantAgent:
    settings = settings or get_settings()
    client = create_model_client("codeur", settings)
    tools = [calculate]
    if settings.code_executor != "disabled":
        tools.insert(0, run_python_snippet)
    return AssistantAgent(
        name=name,
        description="Écrit et exécute du code Python à partir du plan et des retours du Réviseur.",
        model_client=client,
        system_message=prompts.CODEUR,
        tools=tools,
        reflect_on_tool_use=True,
        max_tool_iterations=2,
        model_context=TokenLimitedChatCompletionContext(client, token_limit=settings.context_token_limit),
    )


def create_reviseur(settings: Settings | None = None, name: str = "reviseur") -> AssistantAgent:
    settings = settings or get_settings()
    client = create_model_client("reviseur", settings)
    return AssistantAgent(
        name=name,
        description="Relit le code du Codeur, demande des corrections ou l'APPROUVE.",
        model_client=client,
        system_message=prompts.REVISEUR,
        tools=[check_code_safety],
        reflect_on_tool_use=True,
        model_context=TokenLimitedChatCompletionContext(client, token_limit=settings.context_token_limit),
    )
