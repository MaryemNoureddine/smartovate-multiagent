#!/usr/bin/env bash
# Lancement en un clic (macOS / Linux)
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
.venv/bin/python -m uvicorn api.main:app --port 8000 &
API_PID=$!
trap 'kill $API_PID' EXIT
sleep 4
echo "Interface : http://localhost:8501  (Ctrl+C pour arrêter)"
.venv/bin/python -m streamlit run ui/streamlit_app.py --server.port 8501
