# Smartovate — Système Multi-Agents (Microsoft AutoGen × Azure OpenAI)

Prototype (PoC) d'un système multi-agents. Un **Orchestrateur** découpe la demande, un **Codeur** écrit et exécute du code dans un environnement isolé, un **Réviseur** le relit et un **Critique** valide la qualité et l'absence de données personnelles. Une validation humaine peut être ajoutée en option. L'Orchestrateur consolide ensuite la réponse finale.

- **Framework** : AutoGen v0.4+ (`autogen-agentchat` 0.7.5) : `SelectorGroupChat`, `RoundRobinGroupChat`, `SocietyOfMindAgent`
- **LLM** : Azure OpenAI GPT-4o (2024-11-20), ou un **mode simulation** qui fonctionne sans clé
- **API** : FastAPI (authentification `X-API-Key`, Swagger sur `/docs`)
- **UI** : Streamlit (échanges affichés en direct, validation humaine, export MD/.py/JSON)
- **Mémoire** : Azure Managed Redis (session) et Azure Cosmos DB (audit), avec repli local automatique
- **Déploiement** : Docker / docker-compose / Azure Container Apps (+ Dynamic Sessions pour l'exécution de code)

Le schéma détaillé est dans [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Le déploiement Azure en service (URLs, ressources, validation) est décrit dans [`docs/DEPLOIEMENT_AZURE.md`](docs/DEPLOIEMENT_AZURE.md).

---

## 1. Démarrage rapide (5 minutes, sans Azure)

**Le plus simple :** (Python 3.10 à 3.13 requis, 3.12 recommandé ; Python 3.14 et plus récent n'a pas encore tous les composants précompilés pour Windows) sous Windows, double-cliquez sur `demarrer.bat`. Sous macOS ou Linux, lancez `./demarrer.sh`. Le script installe tout puis ouvre l'interface sur http://localhost:8501.

Installation manuelle :

```bash
python -m venv .venv
# Windows : .venv\Scripts\activate      Linux/macOS : source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # Windows : copy .env.example .env

# Terminal 1 : API
uvicorn api.main:app --reload --port 8000
# Terminal 2 : interface
streamlit run ui/streamlit_app.py
```

Ouvrez http://localhost:8501 et lancez « Écris une fonction Python qui calcule la suite de Fibonacci ».
Sans clé Azure, `LLM_MODE=auto` utilise le **mode simulation**. Toute la chaîne fonctionne (agents, outils, exécution de code, mémoire, export), mais les réponses sont des modèles prédéfinis.

Ligne de commande :

```bash
python -m scripts.cli "Écris une fonction qui fusionne deux listes triées" --pattern nested
```

## 2. Brancher Azure OpenAI (GPT-4o)

1. Portail Azure → votre ressource Azure OpenAI → **Gestion des ressources → Clés et point de terminaison**.
2. Dans `.env` (fichier local, jamais versionné, **jamais copié dans un chat ni sur une capture d'écran**) :

   ```env
   LLM_MODE=azure
   AZURE_OPENAI_ENDPOINT=https://<ressource>.openai.azure.com/
   AZURE_OPENAI_API_KEY=<KEY 1>
   AZURE_OPENAI_DEPLOYMENT=gpt-4o
   AZURE_OPENAI_MODEL_VERSION=2024-11-20
   ```

3. Validez la connexion (US 1.1) : `python -m scripts.test_connection`. Le résultat attendu est `✅ Connexion Azure OpenAI validée`.
4. Redémarrez uvicorn : `--reload` ne relit **pas** le `.env` (voir rapport §9.3).

Redis et Cosmos DB s'activent de la même façon (`REDIS_URL`, `COSMOS_ENDPOINT`, `COSMOS_KEY`), sans modifier le code.

## 3. API REST

Toutes les routes sauf `/health` exigent l'en-tête `X-API-Key`.

| Méthode | Route | Rôle |
|---|---|---|
| POST | `/tasks` | Soumet une tâche `{"task": "...", "pattern": "selector\|sequential\|nested"}` et répond 202 avec `task_id` |
| GET | `/tasks/{id}?since=n` | Statut, messages accumulés (affichage progressif), résultat, code, métriques |
| GET | `/tasks` | Dernières tâches |
| POST | `/tasks/{id}/cancel` | Annule une tâche en cours |
| POST | `/tasks/{id}/human-feedback` | `{"approved": true/false, "comment": "..."}` (Human-in-the-Loop) |
| GET | `/conversations/{id}/export?format=json\|md\|py` | Export de la conversation ou du code |
| GET | `/health?deep=true` | Santé réelle de chaque composant (`healthy` / `degraded` / `unhealthy`) |

Statuts : `EN_ATTENTE → EN_COURS → (EN_ATTENTE_VALIDATION) → TERMINE | ARRETE | ANNULE | ERREUR`.

## 4. Configuration principale (`.env`)

| Variable | Défaut | Effet |
|---|---|---|
| `TEAM_PATTERN` | `selector` | `selector` (hub-and-spoke), `sequential` (pipeline), `nested` (atelier imbriqué) |
| `MAX_MESSAGES` | 15 | Nombre maximal de tours (Bug 1) |
| `MAX_REVIEW_ROUNDS` | 3 | Arrêt anticipé au-delà de N rejets (US 4.2) |
| `MAX_TOKENS_PER_CALL` | 1500 | Limite stricte `max_tokens` pour chaque appel (US 4.2) |
| `MAX_TOTAL_TOKENS` | 60000 | Budget de tokens par tâche |
| `CONTEXT_TOKEN_LIMIT` | 8000 | Troncature de l'historique pour chaque agent (Bug 2 / F4) |
| `CODE_EXECUTOR` | `sandbox` | `sandbox`, `docker` (isolation conteneur), `aca` (Azure Dynamic Sessions), `disabled` (Bug 3) |
| `HUMAN_IN_THE_LOOP` | `false` | Demande une validation humaine avant la clôture (F3) |
| `REDACT_PII_IN_AUDIT` | `true` | Masque les données personnelles dans le journal d'audit |

## 5. Tests et qualité

```bash
pytest --cov                       # 52 tests, couverture ≈ 98 %, aucun appel cloud
python -m scripts.benchmark_performances --url http://localhost:8000   # précision / latence / succès
```

Les tests forcent le mode simulation et remplacent Redis et Cosmos par des doubles de test. Aucune écriture accidentelle en production n'est possible (problème relevé dans le rapport, §11.3).

## 6. Docker et déploiement

```bash
docker build -t smartovate-mas-api .
docker run --env-file .env -p 8000:8000 smartovate-mas-api          # API seule
docker compose up --build                                             # API + UI (8501) + Redis
```

Le `.env` n'est jamais copié dans l'image (`.dockerignore`) et l'image tourne avec un utilisateur non-root.

Le déploiement Azure complet (OpenAI, Cosmos serverless, Managed Redis, ACR, Container Apps, pool Dynamic Sessions, secrets `secretRef`, `minReplicas=0`) se fait avec :

```bash
az login
PREFIX=smartovatemas LOCATION=francecentral bash infra/deploy_azure.sh
```

> Le script n'a pas pu être exécuté sans abonnement Azure. Testez-le d'abord dans un groupe de ressources de test, car les options de `az redisenterprise` évoluent (risque R5).

## 7. Correspondance avec le cahier des charges

| Exigence | Implémentation |
|---|---|
| US 1.1 Connexion Azure OpenAI | `config/model_client.py`, `scripts/test_connection.py`, `.env` |
| US 1.2 Initialisation AutoGen | `requirements.txt` (versions figées), structure des paquets, `scripts/cli.py` |
| US 2.1 Agent Planificateur | `agents/orchestrateur.py` (plan Chain-of-Thought → GroupChat) |
| US 2.2 Codeur / Réviseur | `agents/specialiste.py` : boucle autonome jusqu'à `APPROUVÉ` ou N rejets |
| US 3.1 GroupChat | `agents/team.py` : `SelectorGroupChat` + règles `select_next_speaker`, journaux `logs/` |
| US 3.2 Interface Streamlit | `ui/streamlit_app.py` |
| US 4.1 Docker | `Dockerfile`, `docker-compose.yml`, `.dockerignore` |
| US 4.2 Coûts | prompts concis, `max_tokens`, budget tokens, arrêt anticipé, détection de boucles |
| Bug 1 Boucle infinie | `TextMentionTermination(TERMINATE)` limitée à l'Orchestrateur, `MaxMessage`, `ReviewLoopTermination`, `RepetitionTermination`, `Timeout` |
| Bug 2 Contexte dépassé | `TokenLimitedChatCompletionContext(8000)` par agent et pour le sélecteur, erreur explicite |
| Bug 3 Code non sûr | analyse AST + `sandbox` / `docker` / `aca` (Dynamic Sessions) |

### Écarts du rapport de stage traités dans cette version

- **Patrons Sequential et Nested** (F1) : implémentés, au choix par tâche.
- **Human-in-the-Loop** (F3) : agent `validateur_humain`, endpoint `/human-feedback`, boutons dans l'UI.
- **Troncature à 8 000 tokens** (F4) : configurée et testée.
- **`/health` réel** (MAS-TK-20) : vérifie chaque dépendance, avec un ping LLM via `?deep=true`.
- **Exécution isolée sur Azure** (MAS-TK-21) : mode `aca` avec Dynamic Sessions et un rôle *Session Executor*.
- **Observabilité** (§14.3) : journaux JSON corrélés par `task_id`, métriques par agent, Application Insights en option.
- **Singletons sans connexion à l'import** : `get_session_memory()` et `get_cosmos_logger()` sont créés à la demande.

## 8. Limites connues

- Le mode `sandbox` applique une analyse AST, un sous-processus `-I`, des limites CPU/mémoire et un timeout. Ce n'est **pas** une isolation forte : utilisez `docker` ou `aca` dès que le code n'est pas sûr.
- La détection des données personnelles repose sur des expressions régulières, avec des faux positifs ou négatifs possibles. Azure AI Language (PII) est une évolution possible.
- L'état des tâches en cours est conservé dans le processus. Avec plusieurs réplicas, une file de tâches est nécessaire (Service Bus ou Redis Streams).
