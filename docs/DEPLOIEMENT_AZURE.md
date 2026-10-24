# Déploiement Azure : état au 24/10/2026

**Abonnement :** Smartovate (tenant SMARTOVATE LTD)
**Groupe de ressources :** `maryem-noureddine-smartovate`, région **France Central**
**Créé avec le compte :** maryemnoureddine123@gmail.com (rôle Contributor)

## Accès

| Service | URL |
|---|---|
| Interface Streamlit | https://smartovate-mas-ui.jollyrock-3d6faf55.francecentral.azurecontainerapps.io |
| API (Swagger) | https://smartovate-mas-api.jollyrock-3d6faf55.francecentral.azurecontainerapps.io/docs |
| Santé | https://smartovate-mas-api.jollyrock-3d6faf55.francecentral.azurecontainerapps.io/health?deep=true |

Clé d'API applicative (en-tête `X-API-Key`) : portail Azure → `smartovate-mas-api` → **Secrets** → `api-key`.
Aucun secret ne figure dans le code ni dans ce document.

## Ressources

| Ressource | Nom | Configuration |
|---|---|---|
| Azure OpenAI | `aoai-maryem-noureddine-smartovate` | S0 |
| Déploiement de modèle | `gpt-4o` | version 2024-11-20, GlobalStandard, 50k TPM, mise à jour automatique désactivée |
| Filtre de contenu | `cf-smartovate-mas-strict` | blocage maximal (haine, sexuel, violence, automutilation), Prompt Shields (jailbreak et attaques indirectes), matériel protégé |
| Cosmos DB (NoSQL) | `cosmos-maryem-noureddine-smartovate` | serverless, base `smartovate_mas`, container `conversations` (`/session_id`) |
| Azure Managed Redis | `redis-maryem-noureddine-smartovate` | Balanced_B0, TLS sur le port 10000, authentification par clé |
| Container Registry | `acrmaryemnoureddinesmartovate` | Basic, image `smartovate-mas-api:v1` construite par ACR Tasks |
| Environnement Container Apps | `env-smartovate-mas` | journaux vers Log Analytics |
| Container App API | `smartovate-mas-api` | 1 vCPU / 2 Gi, 0 à 1 réplica, identité managée, secrets `secretRef` |
| Container App UI | `smartovate-mas-ui` | 0,5 vCPU / 1 Gi, 0 à 1 réplica |
| Pool Dynamic Sessions | `pool-smartovate-mas` | PythonLTS, 10 sessions max, sortie réseau désactivée |

## Validation (24/10/2026)

- `/health?deep=true` répond `healthy`, avec le LLM sur Azure, la mémoire de session sur Redis et l'audit sur Cosmos DB.
- Tâche de bout en bout depuis l'interface publique (« fusionner deux listes triées ») : statut TERMINE en 26 s. Orchestrateur, Codeur (exécution), Réviseur, Critique, puis consolidation avec TERMINATE.
- Tâche Fibonacci en local contre GPT-4o : 28 s, 7 678 tokens.

## Action restante (droit Owner requis)

Le compte de déploiement n'a pas le droit d'attribuer des rôles. Tant que ce rôle n'est pas attribué, l'exécution de code tourne en mode `sandbox` dans le conteneur, et non dans le pool Dynamic Sessions. Pour activer l'isolation forte (correctif du Bug 3) :

1. Portail Azure → `pool-smartovate-mas` → **Contrôle d'accès (IAM)** → **Ajouter une attribution de rôle**.
2. Rôle : **Azure ContainerApps Session Executor**. Membre : identité managée → Container App → `smartovate-mas-api`.
3. Puis lancez `az containerapp update -n smartovate-mas-api -g maryem-noureddine-smartovate --set-env-vars CODE_EXECUTOR=aca`, ou modifiez la variable dans le portail.

## Coûts

Coûts fixes : environ 11,5 €/mois pour Redis B0 et environ 4,5 €/mois pour ACR Basic. Le reste est facturé à l'usage : GPT-4o (environ 0,02 à 0,05 € par tâche), Cosmos serverless, Container Apps (0 réplica au repos) et Log Analytics.

Pour tout supprimer : `az group delete -n maryem-noureddine-smartovate`.
