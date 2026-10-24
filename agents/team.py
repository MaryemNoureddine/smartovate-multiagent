"""Assemblage de l'équipe multi-agents (MAS-TK-08, US 3.1).

Trois patrons de topologie (exigence F1), choisis par ``TEAM_PATTERN`` :

- ``selector`` (défaut) : hub-and-spoke via ``SelectorGroupChat``. Le prochain
  locuteur est choisi par des règles déterministes (``select_next_speaker``),
  avec repli sur la sélection par le LLM si aucune règle ne s'applique.
- ``sequential`` : pipeline fixe via ``RoundRobinGroupChat``
  (Orchestrateur -> Codeur -> Réviseur -> Critique -> Orchestrateur ...).
- ``nested`` : l'atelier Codeur + Réviseur est une équipe imbriquée
  (``SocietyOfMindAgent``) vue comme un seul agent par l'équipe principale.
"""
from __future__ import annotations

from typing import Sequence

from autogen_agentchat.agents import SocietyOfMindAgent
from autogen_agentchat.base import Team, TerminationCondition
from autogen_agentchat.conditions import (
    MaxMessageTermination,
    TextMentionTermination,
    TimeoutTermination,
    TokenUsageTermination,
)
from autogen_agentchat.messages import BaseAgentEvent, BaseChatMessage
from autogen_agentchat.teams import RoundRobinGroupChat, SelectorGroupChat
from autogen_core.model_context import TokenLimitedChatCompletionContext

from agents import prompts
from agents.critique import create_critique
from agents.humain import InputFunc, create_validateur_humain
from agents.orchestrateur import create_orchestrateur
from agents.specialiste import create_codeur, create_reviseur
from agents.termination import RepetitionTermination, ReviewLoopTermination
from config.model_client import create_model_client
from config.settings import Settings, get_settings

HUMAN_NAME = "validateur_humain"


def build_termination(settings: Settings) -> TerminationCondition:
    return (
        TextMentionTermination("TERMINATE", sources=["orchestrateur"])
        | MaxMessageTermination(settings.max_messages)
        | TokenUsageTermination(max_total_token=settings.max_total_tokens)
        | TimeoutTermination(timeout_seconds=settings.task_timeout_seconds)
        | ReviewLoopTermination(settings.max_review_rounds)
        | RepetitionTermination()
    )


def make_selector(human_in_the_loop: bool):
    """Règles de prise de parole (speaker selection method) du hub-and-spoke."""

    def select_next_speaker(messages: Sequence[BaseAgentEvent | BaseChatMessage]) -> str | None:
        chat = [m for m in messages if isinstance(m, BaseChatMessage)]
        if not chat:
            return "orchestrateur"
        last = chat[-1]
        text = last.to_text()
        src = last.source
        if src == "user":
            return "orchestrateur"
        if src == "orchestrateur":
            return "codeur"
        if src == "codeur":
            return "reviseur"
        if src == "reviseur":
            if "CORRECTIONS DEMANDÉES" in text:
                return "codeur"
            if "APPROUV" in text.upper():
                return "critique"
            return "codeur"
        if src == "critique":
            if "VERDICT : VALIDÉ" in text or "VERDICT: VALIDÉ" in text:
                return HUMAN_NAME if human_in_the_loop else "orchestrateur"
            return "orchestrateur"
        if src == HUMAN_NAME:
            return "orchestrateur"
        return None  # repli : sélection par le LLM

    return select_next_speaker


def build_team(settings: Settings | None = None, human_input_func: InputFunc | None = None,
               pattern: str | None = None) -> Team:
    settings = settings or get_settings()
    pattern = pattern or settings.team_pattern
    termination = build_termination(settings)
    orchestrateur = create_orchestrateur(settings)
    critique = create_critique(settings)
    human = [create_validateur_humain(human_input_func)] if settings.human_in_the_loop else []

    if pattern == "sequential":
        return RoundRobinGroupChat(
            [orchestrateur, create_codeur(settings), create_reviseur(settings), critique, *human],
            termination_condition=termination,
        )

    if pattern == "nested":
        inner = RoundRobinGroupChat(
            [create_codeur(settings), create_reviseur(settings)],
            termination_condition=TextMentionTermination("APPROUVÉ", sources=["reviseur"])
            | MaxMessageTermination(2 * settings.max_review_rounds + 1),
        )
        atelier = SocietyOfMindAgent(
            "atelier_dev",
            team=inner,
            model_client=create_model_client("atelier", settings),
            description="Atelier de développement imbriqué (Codeur + Réviseur) qui livre un code approuvé.",
            response_prompt=prompts.ATELIER,
        )
        return RoundRobinGroupChat([orchestrateur, atelier, critique, *human], termination_condition=termination)

    selector_client = create_model_client("selector", settings)
    return SelectorGroupChat(
        [orchestrateur, create_codeur(settings), create_reviseur(settings), critique, *human],
        model_client=selector_client,
        termination_condition=termination,
        selector_prompt=prompts.SELECTOR_PROMPT,
        selector_func=make_selector(settings.human_in_the_loop),
        allow_repeated_speaker=False,
        model_context=TokenLimitedChatCompletionContext(selector_client, token_limit=settings.context_token_limit),
    )
