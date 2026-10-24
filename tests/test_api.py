import time

import pytest
from fastapi.testclient import TestClient

from api.main import app, to_markdown

H = {"X-API-Key": "test-key"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def poll(client, task_id, statuses, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.get(f"/tasks/{task_id}", headers=H).json()
        if state["status"] in statuses:
            return state
        time.sleep(0.1)
    raise AssertionError(state["status"])


def test_auth_required(client):
    assert client.post("/tasks", json={"task": "abc"}).status_code == 401
    assert client.get("/tasks", headers={"X-API-Key": "mauvaise"}).status_code == 401


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert body["components"]["llm"]["backend"] == "mock"
    assert client.get("/health?deep=true").json()["components"]["llm"]["status"] == "ok"


def test_task_lifecycle_and_exports(client):
    r = client.post("/tasks", headers=H, json={"task": "Écris une fonction Python qui calcule la suite de Fibonacci"})
    assert r.status_code == 202
    tid = r.json()["task_id"]
    state = poll(client, tid, {"TERMINE", "ERREUR", "ARRETE"})
    assert state["status"] == "TERMINE"
    assert client.get(f"/tasks/{tid}?since=3", headers=H).json()["messages"][0]["index"] == 3
    assert client.get("/tasks", headers=H).json()[0]["id"] == tid

    js = client.get(f"/conversations/{tid}/export", headers=H)
    assert js.status_code == 200 and js.json()["id"] == tid
    md = client.get(f"/conversations/{tid}/export?format=md", headers=H)
    assert "## Résultat" in md.text and "### orchestrateur" in md.text
    py = client.get(f"/conversations/{tid}/export?format=py", headers=H)
    assert "def fibonacci" in py.text
    assert client.post(f"/tasks/{tid}/cancel", headers=H).status_code == 409
    assert client.post(f"/tasks/{tid}/human-feedback", headers=H, json={"approved": True}).status_code == 409


def test_patterns_via_api(client):
    for pattern in ("sequential", "nested"):
        tid = client.post("/tasks", headers=H, json={"task": "Écris une fonction palindrome", "pattern": pattern}).json()["task_id"]
        assert poll(client, tid, {"TERMINE", "ERREUR", "ARRETE"})["status"] == "TERMINE"


def test_validation_and_404(client):
    assert client.post("/tasks", headers=H, json={"task": "x"}).status_code == 422
    assert client.post("/tasks", headers=H, json={"task": "abcd", "pattern": "inconnu"}).status_code == 422
    assert client.get("/tasks/inexistant", headers=H).status_code == 404
    assert client.get("/conversations/inexistant/export", headers=H).status_code == 404
    assert client.post("/tasks/inexistant/cancel", headers=H).status_code == 404


def test_export_py_without_code(client, monkeypatch):
    from core.runner import get_task_manager

    mgr = get_task_manager()
    monkeypatch.setattr(mgr, "get", lambda tid: {"id": tid, "task": "t", "status": "ERREUR", "messages": [], "code": None})
    assert client.get("/conversations/x/export?format=py", headers=H).status_code == 404


def test_to_markdown_includes_events():
    md = to_markdown({"id": "1", "task": "t", "status": "TERMINE", "result": "r", "metrics": {"a": 1},
                      "messages": [{"source": "codeur", "type": "ToolCallRequestEvent", "content": "→ x()", "event": True},
                                   {"source": "codeur", "type": "TextMessage", "content": "ok", "event": False}]})
    assert "> *codeur — ToolCallRequestEvent*" in md and "### codeur" in md and "## Métriques" in md


def test_production_refuses_default_key(monkeypatch):
    from config.settings import get_settings

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("API_KEY", "change-me-dev-key")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError):
        with TestClient(app):
            pass


def test_health_azure_modes(client, monkeypatch):
    from pydantic import SecretStr

    import api.main as main
    from config.settings import get_settings

    s = get_settings()
    s.llm_mode = "azure"
    s.azure_openai_endpoint = "https://exemple.openai.azure.com/"
    s.azure_openai_api_key = SecretStr("fake")
    main._llm_cache.update(at=0.0, value=None)
    assert client.get("/health").json()["components"]["llm"]["detail"].startswith("configuré")

    class FakeClient:
        async def create(self, *a, **k):
            return None

        async def close(self):
            return None

    import config.model_client as mc

    monkeypatch.setattr(mc, "create_model_client", lambda *a, **k: FakeClient())
    body = client.get("/health?deep=true").json()
    assert body["components"]["llm"]["status"] == "ok"
    assert client.get("/health?deep=true").json() == body | {"components": body["components"]}  # cache

    def broken(*a, **k):
        raise ConnectionError("injoignable")

    main._llm_cache.update(at=0.0, value=None)
    monkeypatch.setattr(mc, "create_model_client", broken)
    r = client.get("/health?deep=true")
    assert r.status_code == 503 and r.json()["status"] == "unhealthy"
    main._llm_cache.update(at=0.0, value=None)


def test_health_degraded_when_redis_unreachable(client):
    from config.settings import get_settings
    from memory.redis_memory import reset_session_memory

    get_settings().redis_url = "redis://127.0.0.1:1/0"
    reset_session_memory()
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["components"]["session_memory"]["backend"] == "local"
