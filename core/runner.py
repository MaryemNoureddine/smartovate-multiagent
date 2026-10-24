"""Exécution des tâches multi-agents en arrière-plan.

``TaskManager`` crée l'équipe, consomme ``team.run_stream()`` (affichage
progressif), publie l'état dans la mémoire de session à chaque message,
gère l'annulation et la validation humaine, puis écrit le document d'audit
(conversation complète + journal d'outils + métriques) dans Cosmos DB.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from typing import Any, Optional

from autogen_agentchat.base import TaskResult
from autogen_agentchat.messages import (
    BaseAgentEvent,
    BaseChatMessage,
    ToolCallExecutionEvent,
    ToolCallRequestEvent,
    UserInputRequestedEvent,
)
from autogen_core import CancellationToken

from agents.team import build_team
from config.settings import Settings, get_settings
from core.observability import log_event, task_id_var
from memory.cosmos_logger import get_cosmos_logger
from memory.redis_memory import get_session_memory
from tools.base_tools import detect_pii_sync, redact_pii, tool_calls_var

logger = logging.getLogger(__name__)

CODE_BLOCK_RE = re.compile(r"```python\n(.*?)```", re.DOTALL)
FINAL_STATUSES = {"TERMINE", "ARRETE", "ANNULE", "ERREUR"}
CODE_SOURCES = {"orchestrateur", "codeur", "atelier_dev"}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def task_key(task_id: str) -> str:
    return f"mas:task:{task_id}"


def extract_code(messages: list[dict]) -> str | None:
    for msg in reversed(messages):
        if msg.get("event") or msg.get("source") not in CODE_SOURCES:
            continue
        blocks = CODE_BLOCK_RE.findall(msg.get("content", ""))
        if blocks:
            return blocks[-1]
    return None


def serialize_message(msg: BaseAgentEvent | BaseChatMessage, index: int) -> dict:
    usage = getattr(msg, "models_usage", None)
    entry: dict[str, Any] = {
        "index": index,
        "source": msg.source,
        "type": type(msg).__name__,
        "content": msg.to_text(),
        "ts": _now(),
        "event": isinstance(msg, BaseAgentEvent),
        "tokens": {"prompt": usage.prompt_tokens, "completion": usage.completion_tokens} if usage else None,
    }
    if isinstance(msg, ToolCallRequestEvent):
        entry["content"] = "\n".join(f"→ {c.name}({c.arguments[:400]})" for c in msg.content)
    elif isinstance(msg, ToolCallExecutionEvent):
        entry["content"] = "\n".join(f"← {r.name} : {r.content[:800]}" for r in msg.content)
    return entry


class TaskManager:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._tasks: dict[str, asyncio.Task] = {}
        self._tokens: dict[str, CancellationToken] = {}
        self._human: dict[str, asyncio.Future[str]] = {}
        self._semaphore = asyncio.Semaphore(self.settings.max_concurrent_tasks)

    # ---------------------------------------------------------------- état
    def get(self, task_id: str) -> dict | None:
        return get_session_memory().get_json(task_key(task_id))

    def _save(self, state: dict) -> None:
        state["updated_at"] = _now()
        get_session_memory().set_json(task_key(state["id"]), state)

    def list(self, limit: int = 20) -> list[dict]:
        out = []
        for tid in get_session_memory().list_index(limit):
            state = self.get(tid)
            if state:
                out.append({k: state.get(k) for k in ("id", "task", "status", "pattern", "created_at", "updated_at")})
        return out

    # ------------------------------------------------------------ soumission
    def submit(self, task: str, pattern: str | None = None) -> dict:
        task_id = str(uuid.uuid4())
        pii = detect_pii_sync(task)
        state = {
            "id": task_id,
            "task": task,
            "pattern": pattern or self.settings.team_pattern,
            "llm_mode": self.settings.effective_llm_mode,
            "human_in_the_loop": self.settings.human_in_the_loop,
            "status": "EN_ATTENTE",
            "created_at": _now(),
            "messages": [],
            "result": None,
            "code": None,
            "stop_reason": None,
            "error": None,
            "awaiting_human": None,
            "input_pii": pii["types"],
            "metrics": {},
        }
        self._save(state)
        get_session_memory().add_to_index(task_id)
        self._tokens[task_id] = CancellationToken()
        self._tasks[task_id] = asyncio.create_task(self._run(state))
        return state

    def cancel(self, task_id: str) -> bool:
        token = self._tokens.get(task_id)
        state = self.get(task_id)
        if token is None or state is None or state["status"] in FINAL_STATUSES:
            return False
        token.cancel()
        fut = self._human.get(task_id)
        if fut and not fut.done():
            fut.set_result("REJET HUMAIN : tâche annulée")
        return True

    def submit_human_feedback(self, task_id: str, approved: bool, comment: str = "") -> bool:
        fut = self._human.get(task_id)
        if fut is None or fut.done():
            return False
        text = "APPROUVÉ HUMAIN" if approved else "REJET HUMAIN"
        fut.set_result(f"{text}{' : ' + comment.strip() if comment.strip() else ''}")
        return True

    async def wait(self, task_id: str, timeout: float | None = None) -> dict | None:
        task = self._tasks.get(task_id)
        if task:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        return self.get(task_id)

    # ----------------------------------------------------------- exécution
    def _human_input_func(self, state: dict):
        async def ask_human(prompt: str, cancellation_token: Optional[CancellationToken] = None) -> str:
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[str] = loop.create_future()
            self._human[state["id"]] = fut
            state["status"] = "EN_ATTENTE_VALIDATION"
            state["awaiting_human"] = {"since": _now(), "code": extract_code(state["messages"])}
            self._save(state)
            try:
                return await asyncio.wait_for(fut, timeout=self.settings.human_timeout_seconds)
            except asyncio.TimeoutError:
                return "REJET HUMAIN : délai de validation dépassé"
            finally:
                self._human.pop(state["id"], None)
                state["status"] = "EN_COURS"
                state["awaiting_human"] = None
                self._save(state)

        return ask_human

    async def _run(self, state: dict) -> None:
        task_id = state["id"]
        task_id_var.set(task_id)
        tool_calls: list[dict] = []
        tool_calls_var.set(tool_calls)
        token = self._tokens[task_id]
        per_agent: dict[str, dict[str, float]] = {}
        start = time.perf_counter()
        last_ts = start

        async with self._semaphore:
            state["status"] = "EN_COURS"
            state["started_at"] = _now()
            self._save(state)
            log_event(logger, "tâche démarrée", pattern=state["pattern"], llm_mode=state["llm_mode"])
            try:
                team = build_team(self.settings, human_input_func=self._human_input_func(state),
                                  pattern=state["pattern"])
                result: TaskResult | None = None
                async for item in team.run_stream(task=state["task"], cancellation_token=token):
                    if isinstance(item, TaskResult):
                        result = item
                        continue
                    if isinstance(item, UserInputRequestedEvent):
                        continue
                    now = time.perf_counter()
                    entry = serialize_message(item, len(state["messages"]))
                    state["messages"].append(entry)
                    stats = per_agent.setdefault(item.source, {"messages": 0, "tokens": 0, "latency_s": 0.0})
                    if not entry["event"]:
                        stats["messages"] += 1
                    if entry["tokens"]:
                        stats["tokens"] += entry["tokens"]["prompt"] + entry["tokens"]["completion"]
                    if item.source != "user" and not entry["event"]:
                        stats["latency_s"] = round(stats["latency_s"] + (now - last_ts), 2)
                        last_ts = now
                    self._save(state)

                stop_reason = (result.stop_reason if result else None) or ""
                state["stop_reason"] = stop_reason
                code = extract_code(state["messages"])
                state["code"] = code
                if "TERMINATE" in stop_reason:
                    state["status"] = "TERMINE"
                    final = next((m for m in reversed(state["messages"])
                                  if m["source"] == "orchestrateur" and not m["event"]), None)
                    state["result"] = (final["content"] if final else "").replace("TERMINATE", "").strip()
                else:
                    state["status"] = "ARRETE"
                    state["result"] = (
                        f"Arrêt anticipé : {stop_reason or 'raison inconnue'}.\n\n"
                        + (f"Dernière version du code disponible :\n\n```python\n{code}```" if code else "")
                    ).strip()
            except asyncio.CancelledError:
                state["status"] = "ANNULE"
                state["stop_reason"] = "Annulée par l'utilisateur"
            except Exception as exc:  # noqa: BLE001
                text = str(exc)
                if "context_length_exceeded" in text or "maximum context length" in text:
                    text = "Fenêtre de contexte dépassée (Bug 2) : réduire CONTEXT_TOKEN_LIMIT ou MAX_MESSAGES. " + text
                if token.is_cancelled():
                    state["status"] = "ANNULE"
                    state["stop_reason"] = "Annulée par l'utilisateur"
                else:
                    state["status"] = "ERREUR"
                    state["error"] = f"{type(exc).__name__}: {text}"[:2000]
                    logger.exception("Échec de la tâche")
            finally:
                chat = [m for m in state["messages"] if not m["event"]]
                state["finished_at"] = _now()
                state["metrics"] = {
                    "duration_s": round(time.perf_counter() - start, 2),
                    "messages": len(chat),
                    "events": len(state["messages"]) - len(chat),
                    "tool_calls": len(tool_calls),
                    "tokens_total": int(sum(a["tokens"] for a in per_agent.values())),
                    "per_agent": per_agent,
                }
                self._save(state)
                self._audit(state, tool_calls)
                log_event(logger, "tâche terminée", status=state["status"], **{
                    k: v for k, v in state["metrics"].items() if k != "per_agent"})
                self._tokens.pop(task_id, None)

    def _audit(self, state: dict, tool_calls: list[dict]) -> None:
        doc = {**state, "session_id": state["id"], "doc_type": "conversation", "tool_calls": tool_calls}
        if self.settings.redact_pii_in_audit:
            doc["task"] = redact_pii(doc["task"])
            if doc.get("result"):
                doc["result"] = redact_pii(doc["result"])
            doc["messages"] = [{**m, "content": redact_pii(m["content"])} for m in doc["messages"]]
        try:
            get_cosmos_logger().log(doc)
        except Exception:  # noqa: BLE001
            logger.exception("Écriture du journal d'audit impossible")


_manager: TaskManager | None = None


def get_task_manager() -> TaskManager:
    global _manager
    if _manager is None:
        _manager = TaskManager()
    return _manager


def reset_task_manager(manager: TaskManager | None = None) -> None:
    global _manager
    _manager = manager
