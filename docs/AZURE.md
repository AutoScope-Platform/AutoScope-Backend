# Running AutoScope on Azure

Production runs on Azure (the TSD's choice). This guide sets up an environment (staging or
production) with the Azure CLI, then lets GitHub Actions deploy it on every merge.

> The commands were written against the Azure CLI docs but have **not yet been run against a
> real subscription**. Run them step by step, and check each step worked before the next.
> Needs: `az` 2.60+ with `az extension add --name containerapp --upgrade`.

## What gets created

| Piece | Azure service | Notes |
| --- | --- | --- |
| API (REST + WebSockets) | Container App `autoscope-api` | External ingress, port 8000, 1-3 replicas (2+ in production) |
| Background worker (Celery + beat) | Container App `autoscope-worker` | No ingress. **Exactly 1 replica**: beat would run scheduled jobs twice with more |
| Migrations | Container Apps job `autoscope-migrate` | Manual trigger; the deploy workflow runs it before each rollout |
| Image registry | Azure Container Registry | Images are built in Azure by `az acr build` |
| Database | Azure Database for PostgreSQL Flexible Server 16 | Needs PostGIS, pg_trgm, pgcrypto allowed (step 4) |
| Redis | Azure Cache for Redis | TLS only (`rediss://`, port 6380): queue, rate limits, live events |
| Files | Storage account + private container `uploads` | Photos, KYC documents, claim evidence, chat files |
| Secrets | Key Vault | The apps read them through a managed identity |
| Logs, errors, metrics | Log Analytics + Application Insights | Container logs arrive automatically; App Insights adds requests and exceptions |

## 0. Names and region

Pick once per environment (all names lowercase; storage and registry names: letters and digits only,
globally unique). Example for staging:

```bash
ENVNAME=staging
LOC=southafricanorth            # or westeurope; check latency from Lagos and NDPA data rules
RG=autoscope-$ENVNAME
LAW=autoscope-$ENVNAME-logs
CAE=autoscope-$ENVNAME-env
ACR=autoscopeacr$ENVNAME        # registry: globally unique
PG=autoscope-$ENVNAME-pg
REDIS=autoscope-$ENVNAME-redis
SA=autoscope${ENVNAME}files     # storage account: globally unique, max 24 chars
KV=autoscope-$ENVNAME-kv
ID=autoscope-$ENVNAME-id
```

## 1. Resource group, logs, Container Apps environment

```bash
az group create --name $RG --location $LOC
az monitor log-analytics workspace create --resource-group $RG --workspace-name $LAW --location $LOC
LAW_ID=$(az monitor log-analytics workspace show -g $RG -n $LAW --query customerId -o tsv)
LAW_KEY=$(az monitor log-analytics workspace get-shared-keys -g $RG -n $LAW --query primarySharedKey -o tsv)
az containerapp env create --name $CAE --resource-group $RG --location $LOC \
  --logs-workspace-id $LAW_ID --logs-workspace-key $LAW_KEY
```

## 2. Registry, and a first image

```bash
az acr create --name $ACR --resource-group $RG --sku Basic
az acr build --registry $ACR --image autoscope-api:bootstrap .     # run from the repo root
```

## 3. Identity the apps use (pull images, read secrets)

```bash
az identity create --name $ID --resource-group $RG
ID_RESOURCE=$(az identity show -g $RG -n $ID --query id -o tsv)
ID_PRINCIPAL=$(az identity show -g $RG -n $ID --query principalId -o tsv)
az role assignment create --assignee-object-id $ID_PRINCIPAL --assignee-principal-type ServicePrincipal \
  --role AcrPull --scope $(az acr show -n $ACR --query id -o tsv)
```

## 4. PostgreSQL

```bash
PG_PASSWORD='<generate a long random password>'
# Staging: Burstable B1ms. Production: e.g. --tier GeneralPurpose --sku-name Standard_D2ds_v5
#   --high-availability ZoneRedundant --backup-retention 14 (point-in-time restore is built in).
az postgres flexible-server create --resource-group $RG --name $PG --location $LOC \
  --version 16 --tier Burstable --sku-name Standard_B1ms --storage-size 32 \
  --admin-user autoscope --admin-password "$PG_PASSWORD" --public-access 0.0.0.0
# The migrations run CREATE EXTENSION for these; Azure only allows extensions listed here.
az postgres flexible-server parameter set --resource-group $RG --server-name $PG \
  --name azure.extensions --value POSTGIS,PG_TRGM,PGCRYPTO
az postgres flexible-server db create --resource-group $RG --server-name $PG --database-name autoscope
```

`--public-access 0.0.0.0` allows connections from Azure services only. For production, put the
server on a virtual network (private access) instead.

Connection string (keep `sslmode=require`):
`postgresql://autoscope:<password>@$PG.postgres.database.azure.com:5432/autoscope?sslmode=require`

Connection limits: B1ms allows about 50 connections. Each process opens up to
`DB_POOL_SIZE + DB_MAX_OVERFLOW` (default 10 + 20). With 2 API replicas and the worker on
B1ms, set `DB_POOL_SIZE=5` and `DB_MAX_OVERFLOW=5`.

## 5. Redis

```bash
# Staging: Basic C0. Production: Standard C1 or higher (replicated).
az redis create --resource-group $RG --name $REDIS --location $LOC --sku Basic --vm-size c0
REDIS_KEY=$(az redis list-keys -g $RG -n $REDIS --query primaryKey -o tsv)
```

`REDIS_URL=rediss://:$REDIS_KEY@$REDIS.redis.cache.windows.net:6380/0` (TLS; the worker turns on
certificate checking for `rediss://` by itself).

## 6. File storage

```bash
az storage account create --name $SA --resource-group $RG --location $LOC \
  --sku Standard_LRS --kind StorageV2 --allow-blob-public-access false --min-tls-version TLS1_2
SA_CONN=$(az storage account show-connection-string -g $RG -n $SA -o tsv)
az storage container create --name uploads --connection-string "$SA_CONN"
```

Files stay private; the API hands out read-only links that expire after 15 minutes.

## 7. Application Insights

```bash
az extension add --name application-insights --upgrade
AI_CONN=$(az monitor app-insights component create --app autoscope-$ENVNAME --location $LOC \
  --resource-group $RG --workspace $(az monitor log-analytics workspace show -g $RG -n $LAW --query id -o tsv) \
  --query connectionString -o tsv)
```

## 8. Key Vault and secrets

```bash
az keyvault create --name $KV --resource-group $RG --location $LOC --enable-rbac-authorization true
KV_ID=$(az keyvault show -n $KV --query id -o tsv)
az role assignment create --assignee-object-id $ID_PRINCIPAL --assignee-principal-type ServicePrincipal \
  --role "Key Vault Secrets User" --scope $KV_ID
# You need "Key Vault Secrets Officer" on the vault yourself to run these:
az keyvault secret set --vault-name $KV --name database-url --value "postgresql://autoscope:$PG_PASSWORD@$PG.postgres.database.azure.com:5432/autoscope?sslmode=require"
az keyvault secret set --vault-name $KV --name redis-url --value "rediss://:$REDIS_KEY@$REDIS.redis.cache.windows.net:6380/0"
az keyvault secret set --vault-name $KV --name jwt-secret --value "$(openssl rand -base64 48)"
az keyvault secret set --vault-name $KV --name storage-connection-string --value "$SA_CONN"
az keyvault secret set --vault-name $KV --name appinsights-connection-string --value "$AI_CONN"
# Later, as each third party goes live: paystack-secret-key, termii-api-key, smtp-password,
# fcm-service-account-json ...
```

## 9. The apps and the migration job

The same secrets and settings for all three (staging keeps the mock drivers until the real keys
exist; see docs/INTEGRATIONS.md):

```bash
KVURL=https://$KV.vault.azure.net/secrets
SECRETS="database-url=keyvaultref:$KVURL/database-url,identityref:$ID_RESOURCE \
redis-url=keyvaultref:$KVURL/redis-url,identityref:$ID_RESOURCE \
jwt-secret=keyvaultref:$KVURL/jwt-secret,identityref:$ID_RESOURCE \
storage-connection-string=keyvaultref:$KVURL/storage-connection-string,identityref:$ID_RESOURCE \
appinsights-connection-string=keyvaultref:$KVURL/appinsights-connection-string,identityref:$ID_RESOURCE"
ENVVARS="ENV=$ENVNAME DATABASE_URL=secretref:database-url REDIS_URL=secretref:redis-url \
JWT_SECRET=secretref:jwt-secret TASK_QUEUE=celery STORAGE_DRIVER=azure \
AZURE_STORAGE_CONNECTION_STRING=secretref:storage-connection-string AZURE_STORAGE_CONTAINER=uploads \
APPLICATIONINSIGHTS_CONNECTION_STRING=secretref:appinsights-connection-string \
DB_POOL_SIZE=5 DB_MAX_OVERFLOW=5 CORS_ORIGINS=https://<admin-dashboard-domain> \
SMS_DRIVER=mock EMAIL_DRIVER=mock PUSH_DRIVER=mock PAYMENT_DRIVER=mock KYC_DRIVER=mock SOCIAL_DRIVER=mock"
IMAGE=$ACR.azurecr.io/autoscope-api:bootstrap
COMMON="--resource-group $RG --environment $CAE --image $IMAGE --registry-server $ACR.azurecr.io \
--registry-identity $ID_RESOURCE --secrets $SECRETS --env-vars $ENVVARS"
# Note: jobs take the identity as --mi-user-assigned, apps as --user-assigned.

# Migrations first (a job: runs once, then stops).
az containerapp job create --name autoscope-migrate $COMMON --mi-user-assigned $ID_RESOURCE \
  --trigger-type Manual \
  --replica-timeout 900 --replica-retry-limit 0 --parallelism 1 --replica-completion-count 1 \
  --command alembic --args "upgrade head"
az containerapp job start --name autoscope-migrate --resource-group $RG

# API (production: --min-replicas 2).
az containerapp create --name autoscope-api $COMMON --user-assigned $ID_RESOURCE \
  --ingress external --target-port 8000 \
  --min-replicas 1 --max-replicas 3 --cpu 0.5 --memory 1Gi

# Worker: exactly one replica (Celery beat inside).
az containerapp create --name autoscope-worker $COMMON --user-assigned $ID_RESOURCE \
  --min-replicas 1 --max-replicas 1 \
  --cpu 0.5 --memory 1Gi \
  --command celery --args "-A app.worker.celery_app worker --beat --loglevel=info"
```

Health probes (portal: the API app, then Containers, then Health probes): **liveness** HTTP `GET /health`,
**readiness** HTTP `GET /health/ready` (checks the database and Redis), port 8000.

Staging only, once, after the first migration: load the demo accounts.

```bash
az containerapp exec --name autoscope-api --resource-group $RG --command "python -m app.seed"
```

Never run the seed in production (every demo account shares a published password). Instead,
create the first Super admin there (see docs/INTEGRATIONS.md, "Going live").

## 10. Let GitHub Actions deploy (OIDC, no stored password)

```bash
APP_ID=$(az ad app create --display-name autoscope-github-deploy --query appId -o tsv)
az ad sp create --id $APP_ID
SP_ID=$(az ad sp show --id $APP_ID --query id -o tsv)
az role assignment create --assignee-object-id $SP_ID --assignee-principal-type ServicePrincipal \
  --role Contributor --scope $(az group show -n $RG --query id -o tsv)
az role assignment create --assignee-object-id $SP_ID --assignee-principal-type ServicePrincipal \
  --role AcrPush --scope $(az acr show -n $ACR --query id -o tsv)
az ad app federated-credential create --id $APP_ID --parameters '{
  "name": "github-'$ENVNAME'",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:Great-NGO/Autoscope-Backend:environment:'$ENVNAME'",
  "audiences": ["api://AzureADTokenExchange"]
}'
```

In GitHub, open **Settings, then Environments**, and create `staging` (and `production`, with required
reviewers). On each environment, add these **variables** (not secrets; none of them is secret):

| Variable | Value |
| --- | --- |
| `AZURE_CLIENT_ID` | `$APP_ID` |
| `AZURE_TENANT_ID` | `az account show --query tenantId -o tsv` |
| `AZURE_SUBSCRIPTION_ID` | `az account show --query id -o tsv` |
| `AZURE_RESOURCE_GROUP` | `$RG` |
| `ACR_NAME` | `$ACR` |
| `API_APP` / `WORKER_APP` / `MIGRATE_JOB` | `autoscope-api` / `autoscope-worker` / `autoscope-migrate` |

Then add the **repository** variable `AZURE_DEPLOY_ENABLED` = `true`. From then on, every push to
`develop` deploys to staging (`.github/workflows/deploy-azure.yml`): checks, image build,
migrations, rollout, readiness check. Production deploys from the **Run workflow** button.

## Before production traffic

- [ ] A KYC vendor driver: production refuses to start with any mock driver, and KYC only has a mock today.
- [ ] Live keys for Paystack, Termii, SMTP, FCM and Google/Apple in Key Vault, with the drivers switched.
- [ ] Postgres: General Purpose tier, zone-redundant HA, backup retention set, private network access.
- [ ] Redis: Standard tier or higher. API `--min-replicas 2`; try live tracking and chat across both replicas.
- [ ] Custom domain and managed certificate on the API; `CORS_ORIGINS` set to the dashboard's domain.
- [ ] Alerts in Azure Monitor: failed requests, 5xx rate, readiness failures, database CPU and connections.
- [ ] Re-run `scripts/loadtest_search.py` against a copy of production-sized data.
- [ ] If Azure Front Door is put in front later, check `client_ip` in `app/core/ratelimit.py`: it trusts the
      last `X-Forwarded-For` entry, which is right behind the Container Apps ingress alone.
