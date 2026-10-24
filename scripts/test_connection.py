"""US 1.1 — valide la connexion à Azure OpenAI (``python -m scripts.test_connection``).

La clé n'est jamais affichée : seuls l'endpoint et le déploiement le sont.
"""
from __future__ import annotations

import asyncio
import sys

from autogen_core.models import UserMessage

from config.model_client import create_model_client
from config.settings import get_settings


async def main() -> int:
    s = get_settings()
    print(f"Mode LLM effectif : {s.effective_llm_mode}")
    if s.effective_llm_mode != "azure":
        print("⚠️  Credentials Azure absents : renseignez AZURE_OPENAI_ENDPOINT et AZURE_OPENAI_API_KEY dans .env")
        return 1
    print(f"Endpoint : {s.azure_openai_endpoint} · déploiement : {s.azure_openai_deployment} · API {s.azure_openai_api_version}")
    client = create_model_client("test", s)
    try:
        res = await client.create([UserMessage(content="Réponds exactement : CONNEXION_OK", source="test")])
        print(f"Réponse du modèle : {res.content}")
        print(f"Tokens : {res.usage.prompt_tokens} + {res.usage.completion_tokens}")
        ok = "CONNEXION_OK" in str(res.content)
        print("✅ Connexion Azure OpenAI validée" if ok else "⚠️ Réponse inattendue")
        return 0 if ok else 2
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Échec : {type(exc).__name__} : {exc}")
        return 3
    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
