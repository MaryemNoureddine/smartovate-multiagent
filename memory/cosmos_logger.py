"""Persistance long-terme et auditabilité (MAS-TK-04 / MAS-TK-15).

Azure Cosmos DB (API NoSQL, clé de partition ``/session_id``) si configuré,
sinon repli sur ``logs/conversations.jsonl``. Base et container sont créés
automatiquement à la première écriture.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class CosmosLogger:
    def __init__(self, settings: Settings | None = None, container: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self._container = container
        self._client = None
        self._lock = threading.Lock()
        self.local_path = Path(self.settings.log_dir) / "conversations.jsonl"
        if self._container is None and self.settings.cosmos_configured:
            try:
                from azure.cosmos import CosmosClient, PartitionKey

                self._client = CosmosClient(
                    self.settings.cosmos_endpoint, credential=self.settings.cosmos_key.get_secret_value()
                )
                db = self._client.create_database_if_not_exists(self.settings.cosmos_database)
                self._container = db.create_container_if_not_exists(
                    id=self.settings.cosmos_container, partition_key=PartitionKey(path="/session_id")
                )
                logger.info("Cosmos DB connecté : persistance d'audit active.")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Cosmos DB indisponible (%s) : repli sur %s", type(exc).__name__, self.local_path)
                self._container = None
        elif self._container is None:
            logger.info("Cosmos DB non configuré : journalisation locale dans %s", self.local_path)

    @property
    def backend(self) -> str:
        return "cosmos" if self._container is not None else "local"

    def log(self, document: dict) -> None:
        """Enregistre un document d'audit (doit contenir ``id`` et ``session_id``)."""
        doc = {**document}
        doc.setdefault("logged_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        if self._container is not None:
            try:
                self._container.upsert_item(json.loads(json.dumps(doc, default=str)))
                return
            except Exception as exc:  # noqa: BLE001
                logger.error("Écriture Cosmos DB échouée (%s), repli local", type(exc).__name__)
        self._log_local(doc)

    def _log_local(self, doc: dict) -> None:
        with self._lock:
            self.local_path.parent.mkdir(parents=True, exist_ok=True)
            with self.local_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(doc, ensure_ascii=False, default=str) + "\n")

    def get(self, doc_id: str, session_id: str) -> dict | None:
        if self._container is not None:
            try:
                return self._container.read_item(item=doc_id, partition_key=session_id)
            except Exception:  # noqa: BLE001
                return None
        if not self.local_path.exists():
            return None
        found = None
        with self.local_path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    doc = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if doc.get("id") == doc_id:
                    found = doc
        return found

    def health(self) -> dict:
        if self._container is None:
            status = "degraded" if self.settings.cosmos_configured else "ok"
            return {"status": status, "backend": "local", "detail": str(self.local_path)}
        start = time.perf_counter()
        try:
            self._container.read()
            return {"status": "ok", "backend": "cosmos", "latency_ms": round((time.perf_counter() - start) * 1000, 1)}
        except Exception as exc:  # noqa: BLE001
            return {"status": "down", "backend": "cosmos", "detail": type(exc).__name__}


_instance: CosmosLogger | None = None


def get_cosmos_logger() -> CosmosLogger:
    global _instance
    if _instance is None:
        _instance = CosmosLogger()
    return _instance


def reset_cosmos_logger(instance: CosmosLogger | None = None) -> None:
    global _instance
    _instance = instance
