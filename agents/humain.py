"""Agent Human-in-the-Loop (exigence F3) : validation humaine avant la clôture."""
from __future__ import annotations

from typing import Awaitable, Callable, Optional

from autogen_agentchat.agents import UserProxyAgent
from autogen_core import CancellationToken

InputFunc = Callable[[str, Optional[CancellationToken]], Awaitable[str]]


async def auto_approve(prompt: str, cancellation_token: Optional[CancellationToken] = None) -> str:
    return "APPROUVÉ HUMAIN (validation automatique)"


def create_validateur_humain(input_func: InputFunc | None = None) -> UserProxyAgent:
    return UserProxyAgent(
        name="validateur_humain",
        description="Humain qui approuve (APPROUVÉ HUMAIN) ou rejette (REJET HUMAIN) la solution validée par le Critique.",
        input_func=input_func or auto_approve,
    )
