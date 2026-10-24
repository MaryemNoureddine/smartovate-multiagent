"""Évaluation des performances (précision, latence, taux de succès) contre l'API.

    python -m scripts.benchmark_performances --url http://localhost:8000 [--runs 1]

Écrit un fichier de résultats horodaté dans ``logs/benchmark_<date>.json``.
La « précision » est mesurée en exécutant le code produit dans le bac à sable
avec un test de référence par tâche.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

import httpx

from config.settings import get_settings
from tools.code_execution import execute_code

TASKS = [
    ("T1", "Écris une fonction Python qui calcule la suite de Fibonacci", "assert fibonacci(7) == [0, 1, 1, 2, 3, 5, 8]"),
    ("T2", "Écris une fonction Python qui détecte si une chaîne est un palindrome",
     "assert est_palindrome('Esope reste ici et se repose') and not est_palindrome('abc')"),
    ("T3", "Écris une fonction Python qui trie une liste de dictionnaires selon une clé",
     "assert [d['a'] for d in trier_dictionnaires([{'a': 2}, {'a': 1}], 'a')] == [1, 2]"),
    ("T4", "Écris une fonction Python factorielle avec gestion des cas limites",
     "assert factorielle(0) == 1 and factorielle(5) == 120"),
    ("T5", "Écris une fonction Python qui fusionne deux listes triées",
     "assert fusionner_listes_triees([1, 3], [2, 4]) == [1, 2, 3, 4]"),
]
SLA_SECONDS = 60


async def run_one(client: httpx.AsyncClient, tid: str, task: str, check: str) -> dict:
    start = time.perf_counter()
    r = await client.post("/tasks", json={"task": task})
    r.raise_for_status()
    task_id = r.json()["task_id"]
    while True:
        await asyncio.sleep(1)
        state = (await client.get(f"/tasks/{task_id}")).json()
        if state["status"] in {"TERMINE", "ARRETE", "ANNULE", "ERREUR"}:
            break
    latency = round(time.perf_counter() - start, 1)
    correct = None
    if state.get("code"):
        # La vérification n'est valable que si le LLM a gardé le nom de fonction attendu.
        res = await execute_code(state["code"] + "\n" + check + "\nprint('CHECK_OK')")
        correct = "CHECK_OK" in res.output
    return {"id": tid, "task_id": task_id, "status": state["status"], "latency_s": latency,
            "messages": state["metrics"].get("messages"), "tokens": state["metrics"].get("tokens_total"),
            "correct": correct, "under_sla": latency < SLA_SECONDS}


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=get_settings().api_base_url)
    p.add_argument("--runs", type=int, default=1)
    args = p.parse_args()
    headers = {"X-API-Key": get_settings().api_key.get_secret_value()}
    results = []
    async with httpx.AsyncClient(base_url=args.url, headers=headers, timeout=30) as client:
        for run in range(args.runs):
            for tid, task, check in TASKS:
                res = await run_one(client, tid, task, check)
                res["run"] = run + 1
                results.append(res)
                print(f"{tid} run {run + 1}: {res['status']:<8} {res['latency_s']:>6} s  "
                      f"msgs={res['messages']}  correct={res['correct']}")
    lat = [r["latency_s"] for r in results]
    summary = {
        "success_rate": sum(r["status"] == "TERMINE" for r in results) / len(results),
        "accuracy": sum(bool(r["correct"]) for r in results) / len(results),
        "median_latency_s": statistics.median(lat),
        "under_sla": f"{sum(r['under_sla'] for r in results)}/{len(results)}",
    }
    print(json.dumps(summary, indent=2))
    out = Path(get_settings().log_dir) / f"benchmark_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"url": args.url, "summary": summary, "results": results}, indent=2), encoding="utf-8")
    print(f"Résultats : {out}")


if __name__ == "__main__":
    asyncio.run(main())
