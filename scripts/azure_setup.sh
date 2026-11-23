#!/usr/bin/env bash
# Sets up an AutoScope environment on Azure in one go (docs/AZURE.md, steps 1-10).
#
#   az login                        # once, as the person who has access
#   bash scripts/azure_setup.sh     # from the repo root (Git Bash on Windows is fine)
#
# Safe to run again: every step skips what already exists, so if something fails, fix it and
# re-run. Nothing is deleted. Settings can be overridden from the environment, e.g.
#   ENVNAME=staging LOC=westeurope SUFFIX=px bash scripts/azure_setup.sh
#
# Optional:
#   SEED=1           load the demo accounts and data after migrating (staging only, never production)
#   GITHUB_DEPLOY=1  also create the GitHub Actions sign-in (step 10) and print the variables to add
#   CHECK_ONLY=1     only run the access checks, create nothing
#
# Written from the Azure CLI docs; it has not yet been run against a real subscription.
set -euo pipefail

# --- 0. Names and region -------------------------------------------------------------------
ENVNAME="${ENVNAME:-staging}"
LOC="${LOC:-southafricanorth}"            # or westeurope: check latency from Lagos and NDPA rules
GITHUB_REPO="${GITHUB_REPO:-Paralexapp/AutoScope-Backend}"
SUFFIX="${SUFFIX:-}"                      # added to the globally unique names if they are taken
RG="${RG:-autoscope-$ENVNAME}"
LAW="autoscope-$ENVNAME-logs"
CAE="autoscope-$ENVNAME-env"
ACR="${ACR:-autoscopeacr$ENVNAME$SUFFIX}"         # letters/digits, globally unique
SA="${SA:-autoscope${ENVNAME}files$SUFFIX}"        # letters/digits, max 24, globally unique
PG="${PG:-autoscope-$ENVNAME-pg$SUFFIX}"          # globally unique host name
REDIS="${REDIS:-autoscope-$ENVNAME-redis$SUFFIX}" # globally unique host name
KV="${KV:-autoscope-$ENVNAME-kv$SUFFIX}"          # globally unique, max 24
ID="autoscope-$ENVNAME-id"
API_APP=autoscope-api
WORKER_APP=autoscope-worker
MIGRATE_JOB=autoscope-migrate
SEED_JOB=autoscope-seed
CORS_ORIGINS="${CORS_ORIGINS:-https://autoscope-admin.netlify.app}"
PG_SKU="${PG_SKU:-Standard_B1ms}"
PG_TIER="${PG_TIER:-Burstable}"
REDIS_SKU="${REDIS_SKU:-Basic}"
REDIS_SIZE="${REDIS_SIZE:-c0}"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok() { printf '   ok: %s\n' "$*"; }
fail() { printf '\n\033[31mSTOP: %s\033[0m\n' "$*" >&2; exit 1; }
exists() { "$@" >/dev/null 2>&1; }

[[ "$ENVNAME" == "production" && "${SEED:-0}" == "1" ]] && fail "never seed production (demo accounts share a published password)"
[[ -f Dockerfile && -d app ]] || fail "run this from the repo root"
command -v az >/dev/null || fail "the Azure CLI (az) is not installed"
command -v openssl >/dev/null || fail "openssl is needed to generate secrets (it comes with Git Bash)"

# --- Access checks: fail early with a clear message instead of half-way through ------------
step "Checking your Azure access"
az account show >/dev/null 2>&1 || fail "not signed in: run 'az login' first"
[[ -n "${AZURE_SUBSCRIPTION_ID:-}" ]] && az account set --subscription "$AZURE_SUBSCRIPTION_ID"
SUB_ID=$(az account show --query id -o tsv)
SUB_NAME=$(az account show --query name -o tsv)
ME_ID=$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)
ok "subscription: $SUB_NAME ($SUB_ID)"
[[ -n "$ME_ID" ]] || fail "could not read your user in Entra ID (signed in as a service principal?)"

az extension add --name containerapp --upgrade --only-show-errors >/dev/null
az extension add --name application-insights --upgrade --only-show-errors >/dev/null

PROVIDERS=(Microsoft.App Microsoft.ContainerRegistry Microsoft.DBforPostgreSQL Microsoft.Cache
  Microsoft.Storage Microsoft.KeyVault Microsoft.OperationalInsights Microsoft.Insights
  Microsoft.ManagedIdentity)
missing=()
for p in "${PROVIDERS[@]}"; do
  state=$(az provider show --namespace "$p" --query registrationState -o tsv 2>/dev/null || echo unknown)
  if [[ "$state" != "Registered" ]]; then
    if az provider register --namespace "$p" --wait >/dev/null 2>&1; then ok "registered $p"; else missing+=("$p"); fi
  fi
done
((${#missing[@]} == 0)) || fail "these Azure services are not switched on for the subscription and you cannot switch them on: ${missing[*]}
   Ask the subscription owner to run:  az provider register --namespace <name>  for each."
ok "Azure services switched on"

# Role assignments (images, secrets, GitHub deploy) need Owner, User Access Administrator or
# Role Based Access Control Administrator. Contributor alone cannot grant roles.
if az group exists --name "$RG" | grep -q true; then SCOPE=$(az group show -n "$RG" --query id -o tsv); else SCOPE="/subscriptions/$SUB_ID"; fi
roles=$(az role assignment list --assignee "$ME_ID" --all --include-inherited --include-groups \
  --query "[?starts_with('$SCOPE', scope) || scope=='$SCOPE'].roleDefinitionName" -o tsv 2>/dev/null || true)
grep -qxE "Owner|Contributor" <<<"$roles" || fail "you need Contributor (or Owner) on $SCOPE to create resources. Roles found: ${roles:-none}"
grep -qxE "Owner|User Access Administrator|Role Based Access Control Administrator" <<<"$roles" \
  || fail "you can create resources but not grant permissions, which steps 3, 8 and 10 need.
   Ask for Owner on the resource group $RG (or add 'Role Based Access Control Administrator')."
ok "your roles: $(tr '\n' ',' <<<"$roles" | sed 's/,$//')"

# Globally unique names: check before creating anything.
if ! exists az acr show -n "$ACR"; then
  [[ $(az acr check-name -n "$ACR" --query nameAvailable -o tsv) == true ]] || fail "registry name $ACR is taken: re-run with SUFFIX=<something>"
fi
if ! exists az storage account show -n "$SA" -g "$RG"; then
  ((${#SA} <= 24)) || fail "storage account name $SA is longer than 24 characters"
  [[ $(az storage account check-name -n "$SA" --query nameAvailable -o tsv) == true ]] || fail "storage name $SA is taken: re-run with SUFFIX=<something>"
fi
ok "names: $ACR, $SA, $PG, $REDIS, $KV"
[[ "${CHECK_ONLY:-0}" == "1" ]] && { echo; echo "Access checks passed. Nothing was created (CHECK_ONLY=1)."; exit 0; }

# --- 1. Resource group, logs, Container Apps environment ----------------------------------
step "1. Resource group, logs, Container Apps environment"
az group exists --name "$RG" | grep -q true || az group create --name "$RG" --location "$LOC" -o none
exists az monitor log-analytics workspace show -g "$RG" -n "$LAW" \
  || az monitor log-analytics workspace create -g "$RG" --workspace-name "$LAW" --location "$LOC" -o none
LAW_ID=$(az monitor log-analytics workspace show -g "$RG" -n "$LAW" --query customerId -o tsv)
LAW_RES=$(az monitor log-analytics workspace show -g "$RG" -n "$LAW" --query id -o tsv)
if ! exists az containerapp env show -g "$RG" -n "$CAE"; then
  LAW_KEY=$(az monitor log-analytics workspace get-shared-keys -g "$RG" -n "$LAW" --query primarySharedKey -o tsv)
  az containerapp env create -g "$RG" -n "$CAE" --location "$LOC" \
    --logs-workspace-id "$LAW_ID" --logs-workspace-key "$LAW_KEY" -o none
fi
ok "resource group $RG, environment $CAE"

# --- 2. Registry and a first image ---------------------------------------------------------
step "2. Registry and image (the build runs in Azure, a few minutes)"
exists az acr show -n "$ACR" || az acr create -n "$ACR" -g "$RG" --sku Basic -o none
TAG="setup-$(git rev-parse --short HEAD 2>/dev/null || date +%s)"
if ! exists az acr repository show -n "$ACR" --image "autoscope-api:$TAG"; then
  az acr build --registry "$ACR" --image "autoscope-api:$TAG" . --only-show-errors
fi
IMAGE="$ACR.azurecr.io/autoscope-api:$TAG"
ok "image $IMAGE"

# --- 3. Identity the apps use -------------------------------------------------------------
step "3. Managed identity (pulls images, reads secrets)"
exists az identity show -g "$RG" -n "$ID" || az identity create -g "$RG" -n "$ID" -o none
ID_RESOURCE=$(az identity show -g "$RG" -n "$ID" --query id -o tsv)
ID_PRINCIPAL=$(az identity show -g "$RG" -n "$ID" --query principalId -o tsv)
ACR_ID=$(az acr show -n "$ACR" --query id -o tsv)
grant() { # principal, principal type, role, scope
  if [[ -z $(az role assignment list --assignee "$1" --role "$3" --scope "$4" --query "[0].id" -o tsv) ]]; then
    az role assignment create --assignee-object-id "$1" --assignee-principal-type "$2" --role "$3" --scope "$4" -o none
  fi
}
grant "$ID_PRINCIPAL" ServicePrincipal AcrPull "$ACR_ID"
ok "identity $ID can pull images"

# --- 4. PostgreSQL --------------------------------------------------------------------------
step "4. PostgreSQL 16 (takes 5-10 minutes the first time)"
KV_URI="https://$KV.vault.azure.net"
if ! exists az postgres flexible-server show -g "$RG" -n "$PG"; then
  PG_PASSWORD="$(openssl rand -base64 36 | tr -d '/+=' | cut -c1-32)A1"
  az postgres flexible-server create -g "$RG" -n "$PG" --location "$LOC" --version 16 \
    --tier "$PG_TIER" --sku-name "$PG_SKU" --storage-size 32 \
    --admin-user autoscope --admin-password "$PG_PASSWORD" --public-access 0.0.0.0 --yes -o none
  NEW_PG_PASSWORD=1
else
  NEW_PG_PASSWORD=0
fi
az postgres flexible-server parameter set -g "$RG" --server-name "$PG" \
  --name azure.extensions --value POSTGIS,PG_TRGM,PGCRYPTO -o none
exists az postgres flexible-server db show -g "$RG" --server-name "$PG" --database-name autoscope \
  || az postgres flexible-server db create -g "$RG" --server-name "$PG" --database-name autoscope -o none
ok "server $PG, database autoscope, extensions allowed"

# --- 5. Redis -------------------------------------------------------------------------------
step "5. Redis (takes 15-20 minutes the first time)"
exists az redis show -g "$RG" -n "$REDIS" \
  || az redis create -g "$RG" -n "$REDIS" --location "$LOC" --sku "$REDIS_SKU" --vm-size "$REDIS_SIZE" -o none
REDIS_KEY=$(az redis list-keys -g "$RG" -n "$REDIS" --query primaryKey -o tsv)
ok "redis $REDIS"

# --- 6. File storage ------------------------------------------------------------------------
step "6. File storage (private)"
exists az storage account show -g "$RG" -n "$SA" \
  || az storage account create -n "$SA" -g "$RG" --location "$LOC" --sku Standard_LRS --kind StorageV2 \
       --allow-blob-public-access false --min-tls-version TLS1_2 -o none
SA_CONN=$(az storage account show-connection-string -g "$RG" -n "$SA" -o tsv)
az storage container create --name uploads --connection-string "$SA_CONN" -o none
ok "storage $SA, private container uploads"

# --- 7. Application Insights ----------------------------------------------------------------
step "7. Application Insights"
exists az monitor app-insights component show -g "$RG" --app "autoscope-$ENVNAME" \
  || az monitor app-insights component create -g "$RG" --app "autoscope-$ENVNAME" --location "$LOC" \
       --workspace "$LAW_RES" -o none
AI_CONN=$(az monitor app-insights component show -g "$RG" --app "autoscope-$ENVNAME" --query connectionString -o tsv)
ok "app insights autoscope-$ENVNAME"

# --- 8. Key Vault and secrets ---------------------------------------------------------------
step "8. Key Vault and secrets"
exists az keyvault show -n "$KV" || az keyvault create -n "$KV" -g "$RG" --location "$LOC" --enable-rbac-authorization true -o none
KV_ID=$(az keyvault show -n "$KV" --query id -o tsv)
grant "$ID_PRINCIPAL" ServicePrincipal "Key Vault Secrets User" "$KV_ID"
grant "$ME_ID" User "Key Vault Secrets Officer" "$KV_ID"
secret_exists() { az keyvault secret show --vault-name "$KV" --name "$1" >/dev/null 2>&1; }
put_secret() { az keyvault secret set --vault-name "$KV" --name "$1" --value "$2" -o none; }
for _ in 1 2 3 4 5 6; do  # a new role takes a minute or two to apply
  az keyvault secret list --vault-name "$KV" -o none 2>/dev/null && break
  echo "   waiting for the Key Vault permission to apply..."; sleep 20
done
if [[ "$NEW_PG_PASSWORD" == 1 ]] || ! secret_exists database-url; then
  [[ "$NEW_PG_PASSWORD" == 1 ]] || fail "the database exists but its password is not in Key Vault: reset it with
   az postgres flexible-server update -g $RG -n $PG --admin-password <new> and re-run"
  put_secret database-url "postgresql://autoscope:$PG_PASSWORD@$PG.postgres.database.azure.com:5432/autoscope?sslmode=require"
fi
put_secret redis-url "rediss://:$REDIS_KEY@$REDIS.redis.cache.windows.net:6380/0"
secret_exists jwt-secret || put_secret jwt-secret "$(openssl rand -base64 48)"
put_secret storage-connection-string "$SA_CONN"
put_secret appinsights-connection-string "$AI_CONN"
unset PG_PASSWORD REDIS_KEY SA_CONN AI_CONN
ok "secrets stored in $KV (nothing printed)"

# --- 9. Migration job, API and worker -------------------------------------------------------
step "9. Migrations, API and worker"
KVURL="$KV_URI/secrets"
SECRETS=(
  "database-url=keyvaultref:$KVURL/database-url,identityref:$ID_RESOURCE"
  "redis-url=keyvaultref:$KVURL/redis-url,identityref:$ID_RESOURCE"
  "jwt-secret=keyvaultref:$KVURL/jwt-secret,identityref:$ID_RESOURCE"
  "storage-connection-string=keyvaultref:$KVURL/storage-connection-string,identityref:$ID_RESOURCE"
  "appinsights-connection-string=keyvaultref:$KVURL/appinsights-connection-string,identityref:$ID_RESOURCE"
)
ENVVARS=(
  "ENV=$ENVNAME" "DATABASE_URL=secretref:database-url" "REDIS_URL=secretref:redis-url"
  "JWT_SECRET=secretref:jwt-secret" "TASK_QUEUE=celery" "STORAGE_DRIVER=azure"
  "AZURE_STORAGE_CONNECTION_STRING=secretref:storage-connection-string" "AZURE_STORAGE_CONTAINER=uploads"
  "APPLICATIONINSIGHTS_CONNECTION_STRING=secretref:appinsights-connection-string"
  "DB_POOL_SIZE=5" "DB_MAX_OVERFLOW=5" "CORS_ORIGINS=$CORS_ORIGINS"
  "SMS_DRIVER=mock" "EMAIL_DRIVER=mock" "PUSH_DRIVER=mock" "PAYMENT_DRIVER=mock"
  "KYC_DRIVER=mock" "SOCIAL_DRIVER=mock"
)
COMMON=(--resource-group "$RG" --image "$IMAGE" --registry-server "$ACR.azurecr.io"
  --registry-identity "$ID_RESOURCE" --secrets "${SECRETS[@]}" --env-vars "${ENVVARS[@]}")

run_job() { # name: start it and wait until it finishes
  local execution status
  execution=$(az containerapp job start -n "$1" -g "$RG" --query name -o tsv)
  for _ in $(seq 1 90); do
    status=$(az containerapp job execution show -n "$1" -g "$RG" --job-execution-name "$execution" --query properties.status -o tsv)
    case "$status" in
      Succeeded) ok "$1 finished"; return 0 ;;
      Failed|Stopped|Degraded) fail "$1 $status: see its logs in the portal (Container Apps job $1, Execution history)" ;;
    esac
    sleep 10
  done
  fail "$1 still running after 15 minutes"
}
make_job() { # name, command, args
  if exists az containerapp job show -n "$1" -g "$RG"; then
    az containerapp job update -n "$1" -g "$RG" --image "$IMAGE" -o none
  else
    az containerapp job create -n "$1" --environment "$CAE" "${COMMON[@]}" --mi-user-assigned "$ID_RESOURCE" \
      --trigger-type Manual --replica-timeout 900 --replica-retry-limit 0 --parallelism 1 \
      --replica-completion-count 1 --command "$2" --args "$3" -o none
  fi
}
make_job "$MIGRATE_JOB" alembic "upgrade head"
run_job "$MIGRATE_JOB"

if exists az containerapp show -n "$API_APP" -g "$RG"; then
  az containerapp update -n "$API_APP" -g "$RG" --image "$IMAGE" -o none
else
  min_api=1; [[ "$ENVNAME" == "production" ]] && min_api=2
  az containerapp create -n "$API_APP" --environment "$CAE" "${COMMON[@]}" --user-assigned "$ID_RESOURCE" \
    --ingress external --target-port 8000 --min-replicas "$min_api" --max-replicas 3 --cpu 0.5 --memory 1Gi -o none
fi
if exists az containerapp show -n "$WORKER_APP" -g "$RG"; then
  az containerapp update -n "$WORKER_APP" -g "$RG" --image "$IMAGE" -o none
else
  # Exactly one replica: Celery beat runs inside and would fire scheduled jobs twice with more.
  az containerapp create -n "$WORKER_APP" --environment "$CAE" "${COMMON[@]}" --user-assigned "$ID_RESOURCE" \
    --min-replicas 1 --max-replicas 1 --cpu 0.5 --memory 1Gi \
    --command celery --args "-A app.worker.celery_app worker --beat --loglevel=info" -o none
fi
FQDN=$(az containerapp show -n "$API_APP" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)
ok "API at https://$FQDN"

if [[ "${SEED:-0}" == "1" ]]; then
  make_job "$SEED_JOB" python "-m app.seed"
  run_job "$SEED_JOB"
fi

# --- Smoke test -----------------------------------------------------------------------------
step "Checking the API answers"
ready=""
for _ in $(seq 1 30); do
  ready=$(curl -s -o /dev/null -w '%{http_code}' "https://$FQDN/health/ready" || true)
  [[ "$ready" == 200 ]] && break
  sleep 10
done
[[ "$ready" == 200 ]] && ok "https://$FQDN/health/ready answers 200 (database and Redis reachable)" \
  || echo "   WARNING: /health/ready answered '$ready'. Check the API's logs in the portal (Log stream)."

# --- 10. GitHub Actions deploy (optional) ---------------------------------------------------
if [[ "${GITHUB_DEPLOY:-0}" == "1" ]]; then
  step "10. GitHub Actions sign-in for automatic deploys"
  APP_NAME="autoscope-github-deploy-$ENVNAME"
  APP_ID=$(az ad app list --display-name "$APP_NAME" --query "[0].appId" -o tsv)
  [[ -n "$APP_ID" ]] || APP_ID=$(az ad app create --display-name "$APP_NAME" --query appId -o tsv)
  exists az ad sp show --id "$APP_ID" || az ad sp create --id "$APP_ID" -o none
  SP_ID=$(az ad sp show --id "$APP_ID" --query id -o tsv)
  grant "$SP_ID" ServicePrincipal Contributor "$(az group show -n "$RG" --query id -o tsv)"
  grant "$SP_ID" ServicePrincipal AcrPush "$ACR_ID"
  if [[ -z $(az ad app federated-credential list --id "$APP_ID" --query "[?name=='github-$ENVNAME'].name" -o tsv) ]]; then
    az ad app federated-credential create --id "$APP_ID" --parameters "{
      \"name\": \"github-$ENVNAME\",
      \"issuer\": \"https://token.actions.githubusercontent.com\",
      \"subject\": \"repo:$GITHUB_REPO:environment:$ENVNAME\",
      \"audiences\": [\"api://AzureADTokenExchange\"]
    }" -o none
  fi
  cat <<EOF

   In GitHub ($GITHUB_REPO): Settings > Environments > "$ENVNAME", add these variables:
     AZURE_CLIENT_ID        $APP_ID
     AZURE_TENANT_ID        $(az account show --query tenantId -o tsv)
     AZURE_SUBSCRIPTION_ID  $SUB_ID
     AZURE_RESOURCE_GROUP   $RG
     ACR_NAME               $ACR
     API_APP                $API_APP
     WORKER_APP             $WORKER_APP
     MIGRATE_JOB            $MIGRATE_JOB
   Then the repository variable AZURE_DEPLOY_ENABLED = true.
EOF
fi

cat <<EOF

Done. $ENVNAME is at https://$FQDN
  Swagger:   https://$FQDN/docs
  Health:    https://$FQDN/health/ready
Still to do by hand (portal, API app > Containers > Health probes): liveness GET /health and
readiness GET /health/ready on port 8000.
EOF
