"""Observabilité : journaux structurés JSON corrélés par tâche (piste 14.3).

Chaque ligne de log porte le ``task_id`` courant (ContextVar), ce qui permet de
reconstituer le parcours complet d'une tâche à travers les agents. Si
``APPLICATIONINSIGHTS_CONNECTION_STRING`` est défini et que le paquet
``azure-monitor-opentelemetry`` est installé, les traces sont aussi envoyées
vers Azure Application Insights.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from contextvars import ContextVar
from pathlib import Path

task_id_var: ContextVar[str] = ContextVar("task_id", default="-")

_CONFIGURED = False


class CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.task_id = task_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "task_id": getattr(record, "task_id", "-"),
            "msg": record.getMessage(),
        }
        extra = getattr(record, "data", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO", log_dir: str = "logs") -> None:
    """Configure la journalisation (console + fichier JSON Lines). Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    root = logging.getLogger()
    root.setLevel(level.upper())
    flt = CorrelationFilter()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(task_id)s] %(name)s: %(message)s"))
    console.addFilter(flt)
    root.addHandler(console)

    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(Path(log_dir) / "app.jsonl", encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        fh.addFilter(flt)
        root.addHandler(fh)
    except OSError:  # pragma: no cover - système de fichiers en lecture seule
        root.warning("Impossible d'écrire dans %s, journal fichier désactivé", log_dir)

    # Bibliothèques bavardes
    for noisy in ("azure.core.pipeline.policies.http_logging_policy", "azure", "httpx", "autogen_core", "autogen_agentchat"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _setup_app_insights()
    _CONFIGURED = True


def _setup_app_insights() -> None:
    from config.settings import get_settings

    conn = get_settings().applicationinsights_connection_string
    if not conn:
        return
    try:  # pragma: no cover - dépendance optionnelle
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(connection_string=conn)
        logging.getLogger(__name__).info("Application Insights activé")
    except ImportError:  # pragma: no cover
        logging.getLogger(__name__).warning(
            "APPLICATIONINSIGHTS_CONNECTION_STRING défini mais azure-monitor-opentelemetry non installé"
        )


def log_event(logger: logging.Logger, msg: str, **data: object) -> None:
    """Journalise un événement structuré (champs additionnels dans le JSON)."""
    logger.info(msg, extra={"data": data})
