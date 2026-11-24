# Setting up staging on Azure: the walkthrough

A step-by-step script for doing the Azure setup live, on a call with the client. Part A is what
the client clicks in the Azure portal (about 15 minutes). Part B is what we run (about an hour,
mostly waiting). Each step says what you should see, so you know it worked before moving on.

The technical reference behind this is [AZURE.md](AZURE.md); the script that does the work is
`scripts/azure_setup.sh`.

**Before the call, check:**
- [ ] Your computer has the project on the latest `develop` and the Azure CLI (`az --version` works)
- [ ] You know the **email address** you'll use to sign in to Azure
- [ ] The client has their Azure sign-in to hand, and is an **Owner** of the subscription

---

## Part A: the client, in the Azure portal (about 15 minutes)

Share screen and guide them through it.

### A1. Sign in and find the subscription
1. Go to **https://portal.azure.com** and sign in.
2. In the top search bar type **Subscriptions** and open it.
3. Note the **subscription name** (e.g. "Pay-As-You-Go" or "Paralex Dev").

✅ You should see at least one subscription with status **Active**. If there is none, the
account has no billing set up yet: stop here, it needs a subscription first.

### A2. Give you access to Azure (only if your email isn't already in their Azure)
If you are not already a user in the client's Azure:
1. Search bar: **Microsoft Entra ID** → **Users** → **New user** → **Invite external user**.
2. Enter **your email address**, then **Review + invite** → **Invite**.
3. **You:** open the invitation email and click **Accept invitation**. Sign in when asked.

✅ You should land on a "My Apps" or Azure page saying you belong to their organisation.

### A3. Create a resource group just for AutoScope
A resource group is a folder that holds everything AutoScope uses. Keeping it separate means
we can only touch AutoScope, nothing else in their account.
1. Search bar: **Resource groups** → **Create**.
2. Subscription: the one from A1.
3. Resource group name: **`autoscope-staging`** (exactly this).
4. Region: **South Africa North** (closest to Nigeria; West Europe also works).
5. **Review + create** → **Create**.

✅ "autoscope-staging" appears in the Resource groups list.

### A4. Make you Owner of that resource group
**Owner, not Contributor:** setting up gives the app its own permissions (reading its secrets,
pulling its code, letting GitHub deploy), and only Owner can do that.
1. Open **autoscope-staging** → **Access control (IAM)** (left menu) → **Add** → **Add role assignment**.
2. Tab **Privileged administrator roles** → select **Owner** → **Next**.
3. Assign access to: **User, group, or service principal** → **Select members** → search
   **your email** → **Select**.
4. **Next**. If asked what the user may do, choose **"Allow user to assign all roles except
   privileged administrator roles Owner, UAA, RBAC (Recommended)"**.
5. **Review + assign** (twice).

✅ Under **Access control (IAM)** → **Role assignments**, your email shows with **Owner**.

### A5. Switch on the Azure services AutoScope uses
Azure keeps some services off until they're switched on for the subscription. This needs the
subscription owner, so the client does it.
1. Search bar: **Subscriptions** → open the subscription → left menu **Settings** →
   **Resource providers**.
2. For each name below: type it in the filter box, tick it, click **Register**.
   Skip any that already say **Registered**.

| Provider | What it's for |
| --- | --- |
| `Microsoft.App` | The servers running the API and worker (Container Apps) |
| `Microsoft.ContainerRegistry` | Storing the app's built code |
| `Microsoft.DBforPostgreSQL` | The database |
| `Microsoft.Cache` | Redis (live updates, background jobs) |
| `Microsoft.Storage` | Photos and documents |
| `Microsoft.KeyVault` | Secrets (passwords, keys) |
| `Microsoft.OperationalInsights` | Logs |
| `Microsoft.Insights` | Monitoring and error tracking |
| `Microsoft.ManagedIdentity` | The app's own identity for reading secrets |

✅ All nine show **Registered** (it can take a few minutes; refresh).

**Part A is done.** The client can stay on the call, but nothing else needs them.

---

## Part B: you and Claude, on your computer (about an hour)

### B1. Open the terminal in the project
In VS Code (or Git Bash), open the **Autoscope-Backend** folder and a **Git Bash** terminal.
Make sure you're up to date:
```bash
git switch develop
```
```bash
git pull
```

### B2. Sign in to the client's Azure
```bash
az login
```
A browser window opens: sign in with **your email** (the one given Owner in A4). If the client
invited you as a guest (A2) and the wrong directory opens, use their tenant instead:
`az login --tenant <their-domain.com>`.

Then choose their subscription if asked, and check:
```bash
az account show --query "{subscription:name, user:user.name}" -o table
```
✅ Shows **their** subscription name and **your** email.

### B3. Access check (creates nothing)
```bash
CHECK_ONLY=1 bash scripts/azure_setup.sh
```
✅ Ends with **"Access checks passed. Nothing was created."**

If it stops instead, it says why:

| Message starts with | Fix |
| --- | --- |
| `not signed in` | Run `az login` again (B2) |
| `these Azure services are not switched on` | The client registers the listed ones (A5) |
| `you can create resources but not grant permissions` | The client gives you **Owner** (A4), not Contributor |
| `you need Contributor (or Owner)` | You're on the wrong subscription, or A4 wasn't saved |
| `registry name ... is taken` / `storage name ... is taken` | Re-run with a short suffix: `SUFFIX=px CHECK_ONLY=1 bash scripts/azure_setup.sh` (then use the same `SUFFIX=px` in B4) |

### B4. Build staging
```bash
SEED=1 GITHUB_DEPLOY=1 bash scripts/azure_setup.sh
```
Leave it running. What you'll see, and roughly how long each takes:

| Step | Takes about |
| --- | --- |
| 1. Resource group, logs, environment | 3-5 min |
| 2. Registry and image (builds the app in Azure) | 5-8 min |
| 3. Managed identity | 1 min |
| 4. PostgreSQL | 5-10 min |
| 5. Redis | 15-20 min (the longest; this is normal) |
| 6. File storage | 1 min |
| 7. Application Insights | 1 min |
| 8. Key Vault and secrets | 1-3 min (may say "waiting for the Key Vault permission") |
| 9. Migrations, API, worker, demo data | 5-10 min |
| Checking the API answers | up to 5 min |
| 10. GitHub deploy sign-in | 1 min |

✅ Ends with **"Done. staging is at https://autoscope-api.….azurecontainerapps.io"**, plus a list
of GitHub variables. **Copy that whole output into a note**: you need it in B6.

If it stops part-way, read the STOP message, fix it, and **run the same command again**: it
skips everything already created and carries on.

### B5. Health checks (in the portal, 2 minutes)
These let Azure restart the API automatically if it ever gets stuck.
1. Portal: **autoscope-staging** → **autoscope-api** → **Containers** (left menu) → **Edit and deploy**
   → click the container → **Health probes** tab.
2. **Liveness probe:** enable → HTTP → path **`/health`** → port **8000**.
3. **Readiness probe:** enable → HTTP → path **`/health/ready`** → port **8000**.
4. **Save**, then **Create** (this makes a new revision of the API).

### B6. Turn on automatic deploys from GitHub
Needs someone with **admin** on the GitHub repo (the client, or whoever created it).
1. GitHub → **AutoScope-Platform/AutoScope-Backend** → **Settings** → **Environments** → **New environment** → name **`staging`**.
2. In that environment, under **Environment variables**, add each variable from the B4 output:
   `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`,
   `ACR_NAME`, `API_APP`, `WORKER_APP`, `MIGRATE_JOB`.
3. **Settings** → **Secrets and variables** → **Actions** → **Variables** tab → **New repository variable**:
   `AZURE_DEPLOY_ENABLED` = `true`.

✅ From now on every merge into `develop` deploys to staging by itself (GitHub → **Actions** →
"Deploy (Azure)" shows green).

### B7. Check it works
1. Open **`https://<the address from B4>/docs`**: the API docs (Swagger) load.
2. Open **`https://<address>/health/ready`**: shows the database and Redis as OK.
3. In Swagger: `POST /api/v1/auth/login` with `kunle@demo.ng` / `AutoScope#2026`, then
   `POST /api/v1/auth/login/verify` with code `123456`. You get tokens.
4. Optional, from your computer: run the full scripted demo (parts order, booking, SOS tow,
   insurance) against staging: `BASE_URL=https://<address> make demo`. Claude can run this for you.

### B8. Hand over to the mobile developer
Send them:
- The **API address** (`https://<address>/api/v1`) and **Swagger** (`https://<address>/docs`)
- The guide: `docs/MOBILE_API_GUIDE.md` in the repo
- The demo logins (password `AutoScope#2026`, code `123456`)

---

## If something goes wrong

| Problem | What to do |
| --- | --- |
| The script stopped with a STOP message | Fix what it says, run the same command again |
| Redis or PostgreSQL "takes forever" | Normal: up to 20 minutes. Leave it |
| `/health/ready` doesn't answer 200 | Portal → autoscope-api → **Log stream**: send Claude the red lines |
| The migration job failed | Portal → autoscope-staging → **autoscope-migrate** → **Execution history** → open the failed run → logs |
| You closed the terminal mid-way | Open it again, `az login`, run the same command: it carries on |
| Want to start completely over | The client deletes the resource group **autoscope-staging** (removes everything inside), then start again at A3 |

## After the call
- Costs: the client can see them under **Cost Management** in the portal. Staging uses the
  smallest sizes (Burstable database, Basic Redis, scale-to-small servers).
- Production is the same walkthrough later with `ENVNAME=production` and bigger sizes (see
  AZURE.md, "Before production traffic"). **Never** use `SEED=1` for production.
