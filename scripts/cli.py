"""Lancement d'une tâche en ligne de commande (US 1.2).

    python -m scripts.cli "Écris une fonction qui calcule la suite de Fibonacci" --pattern selector
"""
from __future__ import annotations

import argparse
import asyncio

from autogen_agentchat.ui import Console

from agents.team import build_team
from config.settings import get_settings
from core.observability import setup_logging


async def main() -> None:
    parser = argparse.ArgumentParser(description="Système multi-agents Smartovate (CLI)")
    parser.add_argument("task", help="Demande en langage naturel")
    parser.add_argument("--pattern", choices=["selector", "sequential", "nested"], default=None)
    args = parser.parse_args()
    settings = get_settings()
    setup_logging("WARNING", settings.log_dir)
    print(f"[LLM : {settings.effective_llm_mode} · patron : {args.pattern or settings.team_pattern}]")
    team = build_team(settings, pattern=args.pattern)
    await Console(team.run_stream(task=args.task), output_stats=True)


if __name__ == "__main__":
    asyncio.run(main())
