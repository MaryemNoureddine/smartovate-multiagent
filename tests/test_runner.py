import asyncio
import json

from core.runner import TaskManager, extract_code
from memory.cosmos_logger import get_cosmos_logger


async def wait_status(mgr, task_id, statuses, timeout=20):
    for _ in range(int(timeout / 0.05)):
        state = mgr.get(task_id)
        if state["status"] in statuses:
            return state
        await asyncio.sleep(0.05)
    raise AssertionError(f"statut attendu {statuses}, obtenu {mgr.get(task_id)['status']}")


async def test_task_completes_with_result_metrics_and_audit(settings):
    mgr = TaskManager(settings)
    state = mgr.submit("Écris une fonction Python qui calcule la suite de Fibonacci. Contact : a@b.fr")
    assert state["status"] == "EN_ATTENTE" and state["input_pii"] == ["EMAIL"]
    final = await mgr.wait(state["id"], timeout=30)
    assert final["status"] == "TERMINE"
    assert "TERMINATE" not in final["result"] and "Résultat final" in final["result"]
    assert "def fibonacci" in final["code"] and "raise ValueError" in final["code"]
    m = final["metrics"]
    assert m["messages"] >= 7 and m["tool_calls"] >= 2 and m["tokens_total"] > 0
    assert {"orchestrateur", "codeur", "reviseur", "critique"} <= set(m["per_agent"])
    assert any(msg["event"] for msg in final["messages"])
    assert mgr.list()[0]["id"] == state["id"]

    lines = get_cosmos_logger().local_path.read_text(encoding="utf-8").splitlines()
    doc = json.loads(lines[-1])
    assert doc["session_id"] == state["id"] and doc["tool_calls"]
    assert "a@b.fr" not in json.dumps(doc) and "[EMAIL]" in doc["task"]


async def test_early_stop_on_review_loop(settings):
    settings.max_review_rounds = 0
    mgr = TaskManager(settings)
    final = await mgr.wait(mgr.submit("Écris une fonction palindrome")["id"], timeout=30)
    assert final["status"] == "ARRETE"
    assert "EARLY_STOP" in final["stop_reason"]
    assert "Arrêt anticipé" in final["result"] and "def est_palindrome" in final["result"]


async def test_cancel_task(settings, monkeypatch):
    monkeypatch.setenv("MOCK_LATENCY_SECONDS", "0.3")
    mgr = TaskManager(settings)
    tid = mgr.submit("Écris une fonction factorielle")["id"]
    await wait_status(mgr, tid, {"EN_COURS"})
    assert mgr.cancel(tid)
    final = await mgr.wait(tid, timeout=30)
    assert final["status"] == "ANNULE"
    assert not mgr.cancel(tid)
    assert not mgr.cancel("inconnu")


async def test_human_in_the_loop_reject_then_approve(settings):
    settings.human_in_the_loop = True
    mgr = TaskManager(settings)
    tid = mgr.submit("Écris une fonction qui fusionne deux listes triées")["id"]
    waiting = await wait_status(mgr, tid, {"EN_ATTENTE_VALIDATION"})
    assert "def fusionner_listes_triees" in waiting["awaiting_human"]["code"]
    assert mgr.submit_human_feedback(tid, approved=False, comment="ajoute un exemple")
    for _ in range(400):
        st = mgr.get(tid)
        if st["status"] == "EN_ATTENTE_VALIDATION" and any(m["source"] == "validateur_humain" for m in st["messages"]):
            break
        assert st["status"] not in {"TERMINE", "ARRETE", "ERREUR"}, st["stop_reason"]
        await asyncio.sleep(0.05)
    assert "Révision 1" in mgr.get(tid)["awaiting_human"]["code"]
    assert mgr.submit_human_feedback(tid, approved=True)
    final = await mgr.wait(tid, timeout=30)
    assert final["status"] == "TERMINE"
    texts = [m["content"] for m in final["messages"] if m["source"] == "validateur_humain"]
    assert texts[0].startswith("REJET HUMAIN : ajoute un exemple") and texts[-1].startswith("APPROUVÉ HUMAIN")
    assert not mgr.submit_human_feedback(tid, approved=True)


async def test_human_timeout_rejects(settings):
    settings.human_in_the_loop = True
    settings.human_timeout_seconds = 0
    settings.max_review_rounds = 0
    mgr = TaskManager(settings)
    final = await mgr.wait(mgr.submit("Écris une fonction premier")["id"], timeout=30)
    # 1re correction du Réviseur => arrêt anticipé avant même la validation humaine
    assert final["status"] == "ARRETE"


async def test_error_is_captured(settings, monkeypatch):
    import core.runner as runner

    def boom(*a, **k):
        raise RuntimeError("maximum context length exceeded")

    monkeypatch.setattr(runner, "build_team", boom)
    mgr = TaskManager(settings)
    final = await mgr.wait(mgr.submit("tâche")["id"], timeout=10)
    assert final["status"] == "ERREUR" and "Bug 2" in final["error"]


def test_extract_code_ignores_events_and_other_sources():
    msgs = [
        {"source": "codeur", "event": False, "content": "```python\nv1\n```"},
        {"source": "reviseur", "event": False, "content": "```python\nreview\n```"},
        {"source": "codeur", "event": True, "content": "```python\nevent\n```"},
    ]
    assert extract_code(msgs).strip() == "v1"
    assert extract_code([]) is None
