#!/usr/bin/env bash
# Provisionnement et déploiement Azure de bout en bout (Azure CLI ≥ 2.60, extension containerapp).
#
#   az login
#   export PREFIX=smartovatemas LOCATION=francecentral
#   bash infra/deploy_azure.sh
#
# Le script est idempotent dans la mesure du possible. Les clés ne sont jamais affichées :
# elles sont lues dans des variables shell et injectées comme *secrets* Container Apps.
# NB : les options de `az redisenterprise` évoluent (cf. rapport §9.3, risque R5) —
# vérifier avec `az redisenterprise create --help` si une commande échoue.
set -euo pipefail

# Valeurs par défaut = déploiement réel du 24/10/2026 (abonnement Smartovate)
PREFIX="${PREFIX:-maryem-noureddine-smartovate}"
LOCATION="${LOCATION:-francecentral}"
RG="${RG:-${PREFIX}}"
AOAI="aoai-${PREFIX}"
COSMOS="cosmos-${PREFIX}"
REDIS="redis-${PREFIX}"
ACR="acr${PREFIX//-/}"
ENV_NAME="${ENV_NAME:-env-smartovate-mas}"
APP="${APP:-smartovate-mas-api}"
UI_APP="${UI_APP:-smartovate-mas-ui}"
POOL="${POOL:-pool-smartovate-mas}"
SUB_ID=$(az account show --query id -o tsv)
az extension add -n containerapp --yes -o none 2>/dev/null || true
az extension add -n redisenterprise --yes -o none 2>/dev/null || true
IMAGE_TAG="${IMAGE_TAG:-v1}"
API_KEY_VALUE="${API_KEY_VALUE:-$(openssl rand -hex 24)}"

echo "==> Groupe de ressources ${RG} (${LOCATION})"
az group create -n "$RG" -l "$LOCATION" -o none

echo "==> Azure OpenAI + déploiement gpt-4o (2024-11-20, GlobalStandard, 50k TPM)"
az cognitiveservices account create -n "$AOAI" -g "$RG" -l "$LOCATION" --kind OpenAI --sku S0 \
  --custom-domain "$AOAI" -o none 2>/dev/null || true
# Filtre de contenu strict (niveau de blocage le plus élevé, Prompt Shields, matériel protégé)
python3 - > /tmp/rai.json <<'PY'
import json
f=[{"name":n,"severityThreshold":"Low","blocking":True,"enabled":True,"source":s}
   for s in ("Prompt","Completion") for n in ("Hate","Sexual","Violence","Selfharm")]
f+=[{"name":"Jailbreak","blocking":True,"enabled":True,"source":"Prompt"},
    {"name":"Indirect Attack","blocking":True,"enabled":True,"source":"Prompt"},
    {"name":"Protected Material Text","blocking":True,"enabled":True,"source":"Completion"},
    {"name":"Protected Material Code","blocking":False,"enabled":True,"source":"Completion"}]
print(json.dumps({"properties":{"basePolicyName":"Microsoft.DefaultV2","mode":"Default","contentFilters":f}}))
PY
AOAI_ID="/subscriptions/${SUB_ID}/resourceGroups/${RG}/providers/Microsoft.CognitiveServices/accounts/${AOAI}"
az rest --method put --url "https://management.azure.com${AOAI_ID}/raiPolicies/cf-smartovate-mas-strict?api-version=2024-10-01" \
  --body @/tmp/rai.json -o none
az rest --method put --url "https://management.azure.com${AOAI_ID}/deployments/gpt-4o?api-version=2024-10-01" \
  --body '{"sku":{"name":"GlobalStandard","capacity":50},"properties":{"model":{"format":"OpenAI","name":"gpt-4o","version":"2024-11-20"},"raiPolicyName":"cf-smartovate-mas-strict","versionUpgradeOption":"NoAutoUpgrade"}}' -o none
AOAI_ENDPOINT=$(az cognitiveservices account show -n "$AOAI" -g "$RG" --query properties.endpoint -o tsv)
AOAI_KEY=$(az cognitiveservices account keys list -n "$AOAI" -g "$RG" --query key1 -o tsv)

echo "==> Cosmos DB (NoSQL, serverless)"
az cosmosdb create -n "$COSMOS" -g "$RG" --locations regionName="$LOCATION" \
  --capabilities EnableServerless -o none 2>/dev/null || true
COSMOS_ENDPOINT=$(az cosmosdb show -n "$COSMOS" -g "$RG" --query documentEndpoint -o tsv)
COSMOS_KEY=$(az cosmosdb keys list -n "$COSMOS" -g "$RG" --query primaryMasterKey -o tsv)

echo "==> Azure Managed Redis (Balanced_B0)"
az redisenterprise create -n "$REDIS" -g "$RG" -l "$LOCATION" --sku Balanced_B0 \
  --public-network-access Enabled --access-keys-auth Enabled --clustering-policy NoCluster -o none 2>/dev/null || true
REDIS_HOST=$(az redisenterprise show -n "$REDIS" -g "$RG" --query hostName -o tsv)
REDIS_KEY=$(az redisenterprise database list-keys --cluster-name "$REDIS" -g "$RG" --query primaryKey -o tsv)
REDIS_URL="rediss://:$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$REDIS_KEY")@${REDIS_HOST}:10000/0"

echo "==> Azure Container Registry + build de l'image dans le cloud"
az acr create -n "$ACR" -g "$RG" -l "$LOCATION" --sku Basic --admin-enabled true -o none 2>/dev/null || true
az acr build -r "$ACR" -t "smartovate-mas-api:${IMAGE_TAG}" . -o none
ACR_SERVER=$(az acr show -n "$ACR" --query loginServer -o tsv)
ACR_USER=$(az acr credential show -n "$ACR" --query username -o tsv)
ACR_PASS=$(az acr credential show -n "$ACR" --query "passwords[0].value" -o tsv)

echo "==> Environnement Container Apps + pool Dynamic Sessions (exécution de code isolée, Bug 3)"
az containerapp env create -n "$ENV_NAME" -g "$RG" -l "$LOCATION" -o none 2>/dev/null || true
az containerapp sessionpool create -n "$POOL" -g "$RG" -l "$LOCATION" \
  --container-type PythonLTS --max-sessions 10 --cooldown-period 300 --network-status EgressDisabled \
  -o none 2>/dev/null || true
POOL_ENDPOINT=$(az containerapp sessionpool show -n "$POOL" -g "$RG" --query properties.poolManagementEndpoint -o tsv)

echo "==> Déploiement de l'API (secrets via secretRef, minReplicas=0 / maxReplicas=1)"
az containerapp create -n "$APP" -g "$RG" --environment "$ENV_NAME" \
  --image "${ACR_SERVER}/smartovate-mas-api:${IMAGE_TAG}" \
  --registry-server "$ACR_SERVER" --registry-username "$ACR_USER" --registry-password "$ACR_PASS" \
  --ingress external --target-port 8000 --min-replicas 0 --max-replicas 1 --cpu 1.0 --memory 2.0Gi --system-assigned \
  --secrets aoai-key="$AOAI_KEY" cosmos-key="$COSMOS_KEY" redis-url="$REDIS_URL" api-key="$API_KEY_VALUE" \
  --env-vars APP_ENV=production LLM_MODE=azure \
    AZURE_OPENAI_ENDPOINT="$AOAI_ENDPOINT" AZURE_OPENAI_API_KEY=secretref:aoai-key \
    AZURE_OPENAI_DEPLOYMENT=gpt-4o AZURE_OPENAI_MODEL_VERSION=2024-11-20 \
    COSMOS_ENDPOINT="$COSMOS_ENDPOINT" COSMOS_KEY=secretref:cosmos-key \
    REDIS_URL=secretref:redis-url API_KEY=secretref:api-key \
    CODE_EXECUTOR=aca ACA_POOL_MANAGEMENT_ENDPOINT="$POOL_ENDPOINT" \
  -o none

echo "==> Droit 'Azure ContainerApps Session Executor' pour l'identité managée de l'API"
PRINCIPAL_ID=$(az containerapp show -n "$APP" -g "$RG" --query identity.principalId -o tsv)
POOL_ID=$(az containerapp sessionpool show -n "$POOL" -g "$RG" --query id -o tsv)
az role assignment create --assignee-object-id "$PRINCIPAL_ID" --assignee-principal-type ServicePrincipal \
  --role "Azure ContainerApps Session Executor" --scope "$POOL_ID" -o none 2>/dev/null || {
    echo "   ⚠ Attribution de rôle impossible (droit Owner requis) : bascule de l'exécution de code en mode sandbox."
    az containerapp update -n "$APP" -g "$RG" --set-env-vars CODE_EXECUTOR=sandbox -o none; }

API_FQDN=$(az containerapp show -n "$APP" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)

echo "==> Déploiement de l'UI Streamlit"
az containerapp create -n "$UI_APP" -g "$RG" --environment "$ENV_NAME" \
  --image "${ACR_SERVER}/smartovate-mas-api:${IMAGE_TAG}" \
  --registry-server "$ACR_SERVER" --registry-username "$ACR_USER" --registry-password "$ACR_PASS" \
  --ingress external --target-port 8501 --min-replicas 0 --max-replicas 1 \
  --cpu 0.5 --memory 1.0Gi --secrets api-key="$API_KEY_VALUE" \
  --env-vars API_BASE_URL="https://${API_FQDN}" API_KEY=secretref:api-key STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 STREAMLIT_SERVER_HEADLESS=true STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
  --command streamlit --args run ui/streamlit_app.py \
  -o none
UI_FQDN=$(az containerapp show -n "$UI_APP" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)

echo
echo "API : https://${API_FQDN}/docs   (santé : https://${API_FQDN}/health?deep=true)"
echo "UI  : https://${UI_FQDN}"
echo "La clé d'API applicative a été stockée comme secret Container Apps (non affichée)."
echo "Pour la lire : az containerapp secret show -n ${APP} -g ${RG} --secret-name api-key --query value -o tsv"
