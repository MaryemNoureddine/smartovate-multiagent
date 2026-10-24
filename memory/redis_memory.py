"""Mémoire de session partagée (MAS-TK-05 / MAS-TK-14).

Azure Managed Redis si ``REDIS_URL`` est défini et joignable (``rediss://...:10000``),
sinon repli automatique sur un dictionnaire en mémoire avec TTL. Le code
appelant ignore l'implémentation concrète (inversion de dépendances).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

TASK_INDEX_KEY = "mas:tasks:index"


class SessionMemory:
    def __init__(self, settings: Settings | None = None, client: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self.ttl = self.settings.redis_ttl_seconds
        self._local: dict[str, tuple[float, str]] = {}
        self._index: list[str] = []
        self._lock = threading.Lock()
        self._redis = client
        if self._redis is None and self.settings.redis_url:
            try:
                import redis

                self._redis = redis.Redis.from_url(
                    self.settings.redis_url, socket_connect_timeout=3, socket_timeout=3, decode_responses=True
                )
                self._redis.ping()
                logger.info("Redis connecté : mémoire de session distribuée active.")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Redis indisponible (%s) : repli sur la mémoire locale.", type(exc).__name__)
                self._redis = None
        elif self._redis is None:
            logger.info("REDIS_URL non défini : mémoire de session locale (mode dégradé).")

    @property
    def backend(self) -> str:
        return "redis" if self._redis is not None else "local"

    # ----------------------------------------------------------------- KV JSON
    def set_json(self, key: str, value: Any) -> None:
        data = json.dumps(value, ensure_ascii=False, default=str)
        if self._redis is not None:
            try:
                self._redis.set(key, data, ex=self.ttl)
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("Écriture Redis échouée (%s), repli local", type(exc).__name__)
        with self._lock:
            self._local[key] = (time.time() + self.ttl, data)

    def get_json(self, key: str) -> Any | None:
        if self._redis is not None:
            try:
                raw = self._redis.get(key)
                if raw is not None:
                    return json.loads(raw)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Lecture Redis échouée (%s), repli local", type(exc).__name__)
        with self._lock:
            item = self._local.get(key)
            if item is None:
                return None
            expires, data = item
            if expires < time.time():
                del self._local[key]
                return None
            return json.loads(data)

    def delete(self, key: str) -> None:
        if self._redis is not None:
            try:
                self._redis.delete(key)
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            self._local.pop(key, None)

    # ------------------------------------------------------------- index tâches
    def add_to_index(self, task_id: str, limit: int = 200) -> None:
        if self._redis is not None:
            try:
                pipe = self._redis.pipeline()
                pipe.lpush(TASK_INDEX_KEY, task_id)
                pipe.ltrim(TASK_INDEX_KEY, 0, limit - 1)
                pipe.execute()
                return
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            self._index.insert(0, task_id)
            del self._index[limit:]

    def list_index(self, limit: int = 50) -> list[str]:
        if self._redis is not None:
            try:
                return list(self._redis.lrange(TASK_INDEX_KEY, 0, limit - 1))
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            return list(self._index[:limit])

    # ----------------------------------------------------------------- santé
    def health(self) -> dict:
        if self._redis is None:
            status = "degraded" if self.settings.redis_url else "ok"
            return {"status": status, "backend": "local", "detail": "mémoire locale (repli)"}
        start = time.perf_counter()
        try:
            self._redis.ping()
            return {"status": "ok", "backend": "redis", "latency_ms": round((time.perf_counter() - start) * 1000, 1)}
        except Exception as exc:  # noqa: BLE001
            return {"status": "down", "backend": "redis", "detail": type(exc).__name__}


_instance: SessionMemory | None = None


def get_session_memory() -> SessionMemory:
    """Singleton paresseux (évite toute connexion réseau à l'import — cf. rapport §11.3)."""
    global _instance
    if _instance is None:
        _instance = SessionMemory()
    return _instance


def reset_session_memory(instance: SessionMemory | None = None) -> None:
    global _instance
    _instance = instance
