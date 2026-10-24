"""Configuration centralisée (pydantic-settings).

Toutes les valeurs sont lues depuis les variables d'environnement ou le fichier
``.env`` (jamais versionné). Voir ``.env.example`` pour la liste complète.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Application -------------------------------------------------------
    app_env: Literal["dev", "test", "production"] = "dev"
    api_key: SecretStr = SecretStr("change-me-dev-key")
    log_level: str = "INFO"
    log_dir: str = "logs"
    max_concurrent_tasks: int = 2

    # --- LLM ---------------------------------------------------------------
    # auto : Azure si les credentials sont présents, sinon simulation (mock)
    llm_mode: Literal["auto", "azure", "mock"] = "auto"
    azure_openai_endpoint: str = ""
    azure_openai_api_key: SecretStr = SecretStr("")
    azure_openai_deployment: str = "gpt-4o"
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_model: str = "gpt-4o"
    azure_openai_model_version: str = "2024-11-20"

    temperature: float = 0.2            # R3 : limiter les hallucinations
    max_tokens_per_call: int = 1500     # US 4.2 : plafond strict par appel API
    context_token_limit: int = 8000     # F4 / Bug 2 : troncature du contexte

    # --- Orchestration -------------------------------------------------------
    team_pattern: Literal["selector", "sequential", "nested"] = "selector"
    max_messages: int = 15              # Bug 1 / R1 : plafond de tours
    max_review_rounds: int = 3          # early stopping Codeur <-> Réviseur
    max_total_tokens: int = 60000       # US 4.2 : budget global par tâche
    task_timeout_seconds: int = 300
    human_in_the_loop: bool = False     # F3 : validation humaine avant clôture
    human_timeout_seconds: int = 600

    # --- Exécution de code (Bug 3) --------------------------------------------
    code_executor: Literal["sandbox", "docker", "aca", "disabled"] = "sandbox"
    code_timeout_seconds: int = 30
    docker_image: str = "python:3.12-slim"
    aca_pool_management_endpoint: str = ""

    # --- Mémoire -----------------------------------------------------------
    redis_url: str = ""
    redis_ttl_seconds: int = 86400
    cosmos_endpoint: str = ""
    cosmos_key: SecretStr = SecretStr("")
    cosmos_database: str = "smartovate_mas"
    cosmos_container: str = "conversations"

    redact_pii_in_audit: bool = True    # F3 : masquage des PII dans les journaux d'audit

    # --- Observabilité -------------------------------------------------------
    applicationinsights_connection_string: str = ""

    # --- UI ----------------------------------------------------------------
    api_base_url: str = Field(default="http://localhost:8000")

    @property
    def azure_configured(self) -> bool:
        return bool(self.azure_openai_endpoint and self.azure_openai_api_key.get_secret_value())

    @property
    def effective_llm_mode(self) -> Literal["azure", "mock"]:
        if self.llm_mode == "auto":
            return "azure" if self.azure_configured else "mock"
        return self.llm_mode

    @property
    def cosmos_configured(self) -> bool:
        return bool(self.cosmos_endpoint and self.cosmos_key.get_secret_value())


@lru_cache
def get_settings() -> Settings:
    return Settings()
