"""Interface Streamlit (US 3.2).

- champ de saisie de la requête et choix du patron de topologie ;
- affichage progressif des échanges entre agents (polling de l'API) ;
- validation humaine (Human-in-the-Loop) et annulation ;
- export du résultat (Markdown, code .py, JSON).

Lancement : ``streamlit run ui/streamlit_app.py``
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from config.settings import get_settings

    _settings = get_settings()
    DEFAULT_URL = os.getenv("API_BASE_URL", _settings.api_base_url)
    DEFAULT_KEY = _settings.api_key.get_secret_value()
except Exception:  # noqa: BLE001 - l'UI peut tourner sans le reste du projet
    DEFAULT_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
    DEFAULT_KEY = os.getenv("API_KEY", "")

AVATARS = {
    "user": "🧑", "orchestrateur": "🧭", "codeur": "💻", "reviseur": "🔍",
    "critique": "🛡️", "validateur_humain": "✅", "atelier_dev": "🏭",
}
STATUS_BADGE = {
    "EN_ATTENTE": "🕓 En attente", "EN_COURS": "⚙️ En cours", "EN_ATTENTE_VALIDATION": "✋ Validation humaine requise",
    "TERMINE": "✅ Terminé", "ARRETE": "⏹️ Arrêt anticipé", "ANNULE": "🚫 Annulé", "ERREUR": "❌ Erreur",
}
FINAL = {"TERMINE", "ARRETE", "ANNULE", "ERREUR"}
EXAMPLES = [
    "Écris une fonction Python qui calcule la suite de Fibonacci",
    "Écris une fonction qui détecte si une chaîne est un palindrome",
    "Écris une fonction qui fusionne deux listes triées",
    "Écris une fonction factorielle qui gère les cas limites",
]

st.set_page_config(page_title="Smartovate — Multi-Agents", page_icon="🤖", layout="wide")

with st.sidebar:
    st.header("Connexion")
    api_url = st.text_input("URL de l'API", DEFAULT_URL)
    api_key = st.text_input("Clé d'API (X-API-Key)", DEFAULT_KEY, type="password")
    pattern = st.selectbox("Patron de topologie", ["selector", "sequential", "nested"],
                           help="selector : hub-and-spoke · sequential : pipeline fixe · nested : atelier imbriqué")
    headers = {"X-API-Key": api_key}
    try:
        h = httpx.get(f"{api_url}/health", timeout=5).json()
        st.caption(f"API : **{h['status']}** · LLM : {h['components']['llm']['backend']} · "
                   f"mémoire : {h['components']['session_memory']['backend']} · "
                   f"audit : {h['components']['audit_store']['backend']}")
    except Exception as exc:  # noqa: BLE001
        st.error(f"API injoignable : {exc}")
    st.divider()
    st.subheader("Historique")
    try:
        for t in httpx.get(f"{api_url}/tasks", headers=headers, params={"limit": 10}, timeout=5).json():
            if st.button(f"{STATUS_BADGE.get(t['status'], t['status'])[:2]} {t['task'][:40]}", key=f"h_{t['id']}"):
                st.session_state.task_id = t["id"]
    except Exception:  # noqa: BLE001
        st.caption("—")

st.title("🤖 Système Multi-Agents — AutoGen × Azure OpenAI")
st.caption("Orchestrateur → Codeur ⇄ Réviseur → Critique → réponse consolidée")

with st.form("submit"):
    example = st.selectbox("Exemple (facultatif)", ["—"] + EXAMPLES)
    task = st.text_area("Votre demande", value="" if example == "—" else example, height=110,
                        placeholder="Décrivez la tâche à confier aux agents…")
    submitted = st.form_submit_button("🚀 Lancer les agents", type="primary")

if submitted:
    if len(task.strip()) < 3:
        st.warning("Saisissez une demande.")
    else:
        try:
            r = httpx.post(f"{api_url}/tasks", headers=headers, json={"task": task, "pattern": pattern}, timeout=15)
            r.raise_for_status()
            st.session_state.task_id = r.json()["task_id"]
        except httpx.HTTPStatusError as exc:
            st.error(f"Erreur {exc.response.status_code} : {exc.response.text}")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Impossible de joindre l'API : {exc}")

task_id = st.session_state.get("task_id")
if not task_id:
    st.info("Soumettez une demande pour voir les agents collaborer en temps réel.")
    st.stop()

try:
    state = httpx.get(f"{api_url}/tasks/{task_id}", headers=headers, timeout=10).json()
except Exception as exc:  # noqa: BLE001
    st.error(f"Lecture de la tâche impossible : {exc}")
    st.stop()

if "status" not in state:
    st.error(state.get("detail", "Tâche introuvable"))
    st.stop()

col1, col2, col3, col4 = st.columns(4)
col1.metric("Statut", STATUS_BADGE.get(state["status"], state["status"]))
chat_msgs = [m for m in state["messages"] if not m.get("event")]
col2.metric("Messages", len(chat_msgs))
metrics = state.get("metrics") or {}
col3.metric("Durée", f"{metrics.get('duration_s', 0)} s" if metrics else "…")
col4.metric("Tokens", metrics.get("tokens_total", "…"))
st.caption(f"Tâche `{task_id}` · patron **{state.get('pattern')}** · LLM **{state.get('llm_mode')}**")
if state.get("input_pii"):
    st.warning(f"Données personnelles détectées dans la demande : {', '.join(state['input_pii'])}")

if state["status"] not in FINAL and st.button("🚫 Annuler la tâche"):
    httpx.post(f"{api_url}/tasks/{task_id}/cancel", headers=headers, timeout=5)

st.subheader("Échanges entre agents")
show_events = st.toggle("Afficher les appels d'outils", value=False)
for m in state["messages"]:
    if m.get("event"):
        if show_events:
            with st.chat_message(m["source"], avatar="🛠️"):
                st.caption(m["type"])
                st.code(m["content"], language="text")
        continue
    with st.chat_message(m["source"], avatar=AVATARS.get(m["source"], "🤖")):
        st.markdown(f"**{m['source']}**")
        st.markdown(m["content"])

if state["status"] == "EN_ATTENTE_VALIDATION":
    st.subheader("✋ Validation humaine")
    comment = st.text_input("Commentaire (facultatif, transmis aux agents en cas de rejet)")
    c1, c2 = st.columns(2)
    if c1.button("✅ Approuver", type="primary"):
        httpx.post(f"{api_url}/tasks/{task_id}/human-feedback", headers=headers,
                   json={"approved": True, "comment": comment}, timeout=5)
        st.rerun()
    if c2.button("↩️ Rejeter et demander des corrections"):
        httpx.post(f"{api_url}/tasks/{task_id}/human-feedback", headers=headers,
                   json={"approved": False, "comment": comment}, timeout=5)
        st.rerun()

if state["status"] in FINAL:
    st.divider()
    if state["status"] == "ERREUR":
        st.error(state.get("error"))
    elif state.get("result"):
        st.markdown(state["result"])
    st.caption(f"Raison d'arrêt : {state.get('stop_reason')}")
    if metrics.get("per_agent"):
        with st.expander("Métriques par agent"):
            st.dataframe(
                [{"agent": a, **v} for a, v in metrics["per_agent"].items()], use_container_width=True, hide_index=True
            )
    e1, e2, e3 = st.columns(3)
    md = httpx.get(f"{api_url}/conversations/{task_id}/export", headers=headers, params={"format": "md"}, timeout=10)
    e1.download_button("⬇️ Exporter (Markdown)", md.text, file_name=f"resultat_{task_id[:8]}.md", mime="text/markdown")
    if state.get("code"):
        e2.download_button("⬇️ Exporter le code (.py)", state["code"], file_name=f"solution_{task_id[:8]}.py",
                           mime="text/x-python")
    js = httpx.get(f"{api_url}/conversations/{task_id}/export", headers=headers, params={"format": "json"}, timeout=10)
    e3.download_button("⬇️ Exporter (JSON)", js.text, file_name=f"conversation_{task_id[:8]}.json",
                       mime="application/json")
elif state["status"] != "EN_ATTENTE_VALIDATION":
    time.sleep(1)
    st.rerun()
