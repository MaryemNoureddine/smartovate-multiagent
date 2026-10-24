"""Conditions d'arrêt (Bug 1 / R1 et early stopping US 4.2).

Combinées dans ``agents.team.build_termination`` :
- TERMINATE émis par l'Orchestrateur (fin normale) ;
- plafond de messages (MAX_MESSAGES) ;
- budget de tokens par tâche (MAX_TOTAL_TOKENS) ;
- délai maximal (TASK_TIMEOUT_SECONDS) ;
- ``ReviewLoopTermination`` : trop de cycles de corrections Codeur <-> Réviseur ;
- ``RepetitionTermination`` : un agent répète le même message (boucle).
"""
from __future__ import annotations

import re
from typing import Sequence

from autogen_agentchat.base import TerminatedException, TerminationCondition
from autogen_agentchat.messages import BaseAgentEvent, BaseChatMessage, StopMessage

REJECTION_MARKERS = ("CORRECTIONS DEMANDÉES", "VERDICT : REJETÉ", "REJET HUMAIN")


class ReviewLoopTermination(TerminationCondition):
    """Arrête la conversation quand le nombre de rejets dépasse ``max_rounds``."""

    def __init__(self, max_rounds: int) -> None:
        self._max_rounds = max_rounds
        self._count = 0
        self._terminated = False

    @property
    def terminated(self) -> bool:
        return self._terminated

    async def __call__(self, messages: Sequence[BaseAgentEvent | BaseChatMessage]) -> StopMessage | None:
        if self._terminated:
            raise TerminatedException("Termination condition has already been reached")
        for msg in messages:
            if isinstance(msg, BaseChatMessage) and msg.source != "user":
                text = msg.to_text()
                if any(marker in text for marker in REJECTION_MARKERS):
                    self._count += 1
        if self._count > self._max_rounds:
            self._terminated = True
            return StopMessage(
                content=f"EARLY_STOP: {self._count} cycles de corrections (> {self._max_rounds}), arrêt anticipé",
                source="ReviewLoopTermination",
            )
        return None

    async def reset(self) -> None:
        self._count = 0
        self._terminated = False


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


class RepetitionTermination(TerminationCondition):
    """Arrête la conversation quand elle tourne en rond : les ``n`` derniers messages
    (source + contenu) sont identiques aux ``n`` précédents, pour n = 1..max_window.
    Ex. : Codeur(A), Réviseur(B), Codeur(A), Réviseur(B) -> boucle."""

    def __init__(self, max_window: int = 4) -> None:
        self._max_window = max_window
        self._history: list[tuple[str, str]] = []
        self._terminated = False

    @property
    def terminated(self) -> bool:
        return self._terminated

    async def __call__(self, messages: Sequence[BaseAgentEvent | BaseChatMessage]) -> StopMessage | None:
        if self._terminated:
            raise TerminatedException("Termination condition has already been reached")
        for msg in messages:
            if not isinstance(msg, BaseChatMessage) or msg.source == "user":
                continue
            self._history.append((msg.source, _normalize(msg.to_text())))
            h = self._history
            for n in range(1, self._max_window + 1):
                if len(h) >= 2 * n and h[-n:] == h[-2 * n:-n]:
                    self._terminated = True
                    sources = " -> ".join(src for src, _ in h[-n:])
                    return StopMessage(
                        content=f"EARLY_STOP: boucle détectée (séquence répétée : {sources})",
                        source="RepetitionTermination",
                    )
        return None

    async def reset(self) -> None:
        self._history = []
        self._terminated = False
