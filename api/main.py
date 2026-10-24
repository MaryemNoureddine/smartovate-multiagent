"""API REST d'exposition du système multi-agents (MAS-TK-16/17).

Endpoints (tous protégés par l'en-tête ``X-API-Key`` sauf ``/health``) :

- ``POST /tasks``                         soumettre une tâche (réponse immédiate, exécution en fond)
- ``GET  /tasks``                         dernières tâches
- ``GET  /tasks/{id}``                    état, messages accumulés (streaming par polling), résultat
- ``POST /tasks/{id}/cancel``             annuler une tâche
- ``POST /tasks/{id}/human-feedback``     approuver / rejeter (Human-in-the-Loop)
- ``GET  /conversations/{id}/export``     export JSON, Markdown ou .py
- ``GET  /health``                        santé réelle des dépendances (piste MAS-TK-20)
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Security, status
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from config.settings import get_settings
from core.observability import setup_logging
from core.runner import get_task_manager
from memory.cosmos_logger import get_cosmos_logger
from memory.redis_memory import get_session_memory

logger = logging.getLogger(__name__)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
VERSION = "1.0.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_dir)
    if settings.app_env == "production" and settings.api_key.get_secret_value() == "change-me-dev-key":
        raise RuntimeError("API_KEY par défaut interdite en production : définissez une clé forte.")
    logger.info("API démarrée (LLM=%s, pattern=%s, exécuteur=%s)", settings.effective_llm_mode,
                settings.team_pattern, settings.code_executor)
    yield


app = FastAPI(
    title="Smartovate — Système Multi-Agents (AutoGen + Azure OpenAI)",
    version=VERSION,
    description="Orchestrateur, Codeur, Réviseur et Critique collaborent pour traiter une demande en langage naturel.",
    lifespan=lifespan,
)


def require_api_key(key: str | None = Security(api_key_header)) -> None:
    expected = get_settings().api_key.get_secret_value()
    if not key or not hmac.compare_digest(key, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Clé d'API invalide ou absente")


class TaskRequest(BaseModel):
    task: str = Field(..., min_length=3, max_length=8000, description="Demande en langage naturel")
    pattern: Literal["selector", "sequential", "nested"] | None = Field(
        default=None, description="Patron de topologie (défaut : TEAM_PATTERN)")


class HumanFeedback(BaseModel):
    approved: bool
    comment: str = Field(default="", max_length=2000)


def _get_or_404(task_id: str) -> dict:
    state = get_task_manager().get(task_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Tâche introuvable")
    return state


@app.post("/tasks", status_code=202, dependencies=[Depends(require_api_key)])
async def create_task(req: TaskRequest) -> dict:
    state = get_task_manager().submit(req.task, req.pattern)
    return {"task_id": state["id"], "status": state["status"], "pattern": state["pattern"],
            "llm_mode": state["llm_mode"]}


@app.get("/tasks", dependencies=[Depends(require_api_key)])
async def list_tasks(limit: int = Query(20, ge=1, le=100)) -> list[dict]:
    return get_task_manager().list(limit)


@app.get("/tasks/{task_id}", dependencies=[Depends(require_api_key)])
async def get_task(task_id: str, since: int = Query(0, ge=0, description="Ne renvoyer que les messages d'index >= since")) -> dict:
    state = _get_or_404(task_id)
    if since:
        state = {**state, "messages": [m for m in state["messages"] if m["index"] >= since]}
    return state


@app.post("/tasks/{task_id}/cancel", dependencies=[Depends(require_api_key)])
async def cancel_task(task_id: str) -> dict:
    _get_or_404(task_id)
    if not get_task_manager().cancel(task_id):
        raise HTTPException(status_code=409, detail="Tâche déjà terminée ou non annulable")
    return {"task_id": task_id, "cancel_requested": True}


@app.post("/tasks/{task_id}/human-feedback", dependencies=[Depends(require_api_key)])
async def human_feedback(task_id: str, fb: HumanFeedback) -> dict:
    _get_or_404(task_id)
    if not get_task_manager().submit_human_feedback(task_id, fb.approved, fb.comment):
        raise HTTPException(status_code=409, detail="Aucune validation humaine en attente pour cette tâche")
    return {"task_id": task_id, "approved": fb.approved}


def to_markdown(state: dict) -> str:
    lines = [f"# Tâche {state['id']}", "", f"**Demande :** {state['task']}", "",
             f"**Statut :** {state['status']} — **Patron :** {state.get('pattern')} — **LLM :** {state.get('llm_mode')}", ""]
    if state.get("result"):
        lines += ["## Résultat", "", state["result"], ""]
    lines += ["## Conversation", ""]
    for m in state["messages"]:
        if m.get("event"):
            lines += [f"> *{m['source']} — {m['type']}*", ">", *[f"> {l}" for l in m["content"].splitlines()], ""]
        else:
            lines += [f"### {m['source']}", "", m["content"], ""]
    if state.get("metrics"):
        lines += ["## Métriques", "", "```json", json.dumps(state["metrics"], indent=2, ensure_ascii=False), "```"]
    return "\n".join(lines)


@app.get("/conversations/{task_id}/export", dependencies=[Depends(require_api_key)])
async def export_conversation(task_id: str, format: Literal["json", "md", "py"] = "json"):
    state = _get_or_404(task_id)
    headers = {"Content-Disposition": f'attachment; filename="conversation_{task_id}.{format}"'}
    if format == "md":
        return PlainTextResponse(to_markdown(state), media_type="text/markdown; charset=utf-8", headers=headers)
    if format == "py":
        if not state.get("code"):
            raise HTTPException(status_code=404, detail="Aucun code produit pour cette tâche")
        return PlainTextResponse(state["code"], media_type="text/x-python; charset=utf-8", headers=headers)
    return JSONResponse(state, headers=headers)


# ------------------------------------------------------------------- santé
_llm_cache: dict = {"at": 0.0, "value": None}


async def _check_llm(deep: bool) -> dict:
    settings = get_settings()
    if settings.effective_llm_mode == "mock":
        return {"status": "ok", "backend": "mock", "detail": "mode simulation"}
    if not deep:
        return {"status": "ok", "backend": "azure", "detail": "configuré (non testé, utiliser ?deep=true)"}
    if _llm_cache["value"] and time.time() - _llm_cache["at"] < 60:
        return _llm_cache["value"]
    from autogen_core.models import UserMessage

    from config.model_client import create_model_client

    start = time.perf_counter()
    try:
        client = create_model_client("health", settings)
        await asyncio.wait_for(
            client.create([UserMessage(content="Réponds OK", source="health")], extra_create_args={"max_tokens": 2}),
            timeout=10,
        )
        await client.close()
        value = {"status": "ok", "backend": "azure", "latency_ms": round((time.perf_counter() - start) * 1000, 1)}
    except Exception as exc:  # noqa: BLE001
        value = {"status": "down", "backend": "azure", "detail": type(exc).__name__}
    _llm_cache.update(at=time.time(), value=value)
    return value


@app.get("/health")
async def health(deep: bool = False) -> JSONResponse:
    components = {
        "llm": await _check_llm(deep),
        "session_memory": await asyncio.to_thread(get_session_memory().health),
        "audit_store": await asyncio.to_thread(get_cosmos_logger().health),
    }
    statuses = {c["status"] for c in components.values()}
    if components["llm"]["status"] == "down":
        overall = "unhealthy"
    elif statuses - {"ok"}:
        overall = "degraded"
    else:
        overall = "healthy"
    code = 503 if overall == "unhealthy" else 200
    return JSONResponse({"status": overall, "version": VERSION, "components": components}, status_code=code)
