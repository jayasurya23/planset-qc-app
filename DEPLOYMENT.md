# Deploying Castillo QAQC Automation to Azure

The app runs as a **single Linux container** on **Azure Container Apps**: FastAPI
serves both the API and the built React UI. The image is built in **Azure
Container Registry (ACR)**; **GitHub Actions** rolls new images out on push.
Access is locked to the organization via **Microsoft Entra** built-in auth.

> Container Apps (rather than App Service) because the subscription has **0
> App Service "VM" quota** — Container Apps uses a separate quota pool. The
> capabilities are equivalent for this workload.

```
GitHub (push to master)
        │  GitHub Actions (OIDC login)
        ▼
   az acr build ─► Azure Container Registry ─► Container App (1 replica, always-on)
                                                 ├─ FastAPI API + React SPA
                                                 ├─ Azure Files mount → /home/data
                                                 │     (SQLite, PDFs, snippets, exports, logs)
                                                 └─ Entra built-in auth (org-only)
                                                       │
                                                  OpenAI API
```

## What's deployed

Resource group **`castillo-qaqc-automation-rg`** (region **East US**):

| Resource | Name |
| --- | --- |
| Container App | `castillo-qaqc-automation` |
| Container Apps environment | `castillo-qaqc-automation-env` |
| Container Registry | `castilloqaqcautomationacr` |
| Storage account (Azure Files) | `st…` (file share `data`) |
| Log Analytics workspace | `castillo-qaqc-automation-logs` |
| User-assigned identity (ACR pull) | `castillo-qaqc-automation-id` |

Live URL: **https://castillo-qaqc-automation.&lt;env-id&gt;.eastus.azurecontainerapps.io**
(get the exact host with `az containerapp show -n castillo-qaqc-automation -g castillo-qaqc-automation-rg --query properties.configuration.ingress.fqdn -o tsv`).

**Single replica, by design.** SQLite lives on the Azure Files (SMB) mount and is
opened with `nolock=1` + an in-process lock (see `backend/app/db.py`). That is
correct only for **one writer**, so the app is pinned to `minReplicas: 1,
maxReplicas: 1`. Do **not** raise `maxReplicas`. To scale horizontally, migrate
the DB to Azure Database for PostgreSQL and artifacts to Blob Storage.

---

## Prerequisites (for re-provisioning from scratch)

- Azure CLI (`az login`), an Azure subscription, Owner on the target RG.
- GitHub CLI (`gh`) authenticated, or use the GitHub UI for secrets.
- Your OpenAI API key.

```powershell
$RG  = "castillo-qaqc-automation-rg"
$LOC = "eastus"
$APP = "castillo-qaqc-automation"
$ACR = "castilloqaqcautomationacr"
az group create -n $RG -l $LOC
```

## 1. Registry + image (must exist before the app)

```powershell
az acr create -n $ACR -g $RG --sku Basic --location $LOC
az acr build -r $ACR -t planset-qc:latest .
```

> On Windows, `az acr build`'s log streamer can crash on a Unicode character
> (`UnicodeEncodeError: charmap`). The build still completes server-side — check
> with `az acr task list-runs -r $ACR --top 1 -o table` and proceed when it
> shows `Succeeded`.

## 2. Deploy the infrastructure

```powershell
az deployment group create -g $RG -f infra/main.bicep -p infra/main.parameters.json `
  -p openAiApiKey="sk-...your-key..."
```

This creates Log Analytics, the storage account + `data` file share, the
managed environment (with the share linked), and the Container App (image pulled
via a user-assigned identity, OpenAI key stored as a secret, models set to
`gpt-5.4-mini` / `gpt-5.4`). Output `appUrl` is the live URL.

Verify it's healthy:

```powershell
$URL = "https://$(az containerapp show -n $APP -g $RG --query properties.configuration.ingress.fqdn -o tsv)"
curl "$URL/api/runs"     # -> [] once running
```

If a revision is unhealthy, read logs:
`az containerapp logs show -n $APP -g $RG --type console --tail 60`.

## 3. Lock access to your organization (Entra)

Built-in auth puts a Microsoft sign-in in front of the whole app — no code change.

```powershell
$FQDN   = az containerapp show -n $APP -g $RG --query properties.configuration.ingress.fqdn -o tsv
$TENANT = az account show --query tenantId -o tsv

# App registration for the sign-in (single-tenant = org-only):
$AUTHID = az ad app create --display-name "Castillo QAQC Automation - Auth" `
  --sign-in-audience AzureADMyOrg `
  --web-redirect-uris "https://$FQDN/.auth/login/aad/callback" --query appId -o tsv
az ad sp create --id $AUTHID
$SECRET = az ad app credential reset --id $AUTHID --query password -o tsv

# Wire it into the Container App and require sign-in:
az containerapp auth microsoft update -n $APP -g $RG `
  --client-id $AUTHID --client-secret $SECRET `
  --issuer "https://login.microsoftonline.com/$TENANT/v2.0" --yes
az containerapp auth update -n $APP -g $RG `
  --unauthenticated-client-action RedirectToLoginPage --redirect-provider azureactivedirectory
```

To later restrict to **specific people** rather than the whole tenant: in Entra →
Enterprise applications → this app → Properties, set **Assignment required = Yes**,
then add users/groups under **Users and groups**.

## 4. GitHub Actions (push-to-deploy)

The workflow ([.github/workflows/deploy.yml](.github/workflows/deploy.yml)) logs
in with OIDC, builds in ACR, and rolls the Container App. One-time setup:

```powershell
$SUBID = az account show --query id -o tsv
$REPO  = "jayasurya23/planset-qc-app"

# Identity GitHub authenticates as:
$CID = az ad app create --display-name "github-castillo-qaqc-deploy" --query appId -o tsv
$OID = az ad sp create --id $CID --query id -o tsv

# Trust pushes to master (save as federated-credential.json):
#   { "name":"github-master", "issuer":"https://token.actions.githubusercontent.com",
#     "subject":"repo:jayasurya23/planset-qc-app:ref:refs/heads/master",
#     "audiences":["api://AzureADTokenExchange"] }
az ad app federated-credential create --id $CID --parameters "@federated-credential.json"

# Contributor on the RG (covers acr build + containerapp update):
az role assignment create --assignee-object-id $OID --assignee-principal-type ServicePrincipal `
  --role Contributor --scope "/subscriptions/$SUBID/resourceGroups/$RG"
```

> If `az role assignment create` fails with `MissingSubscription` (a CLI bug
> seen in some versions), create it via REST instead — PUT to
> `…/resourceGroups/$RG/providers/Microsoft.Authorization/roleAssignments/<new-guid>?api-version=2022-04-01`
> with body `{properties:{roleDefinitionId:".../b24988ac-6180-42a0-ab88-20f7382dd24c", principalId:$OID, principalType:"ServicePrincipal"}}`.

Repo secrets the workflow reads:

```powershell
gh secret set AZURE_CLIENT_ID        --repo $REPO --body $CID
gh secret set AZURE_TENANT_ID        --repo $REPO --body $TENANT
gh secret set AZURE_SUBSCRIPTION_ID  --repo $REPO --body $SUBID
gh secret set AZURE_RESOURCE_GROUP   --repo $REPO --body $RG
gh secret set AZURE_CONTAINERAPP_NAME --repo $REPO --body $APP
gh secret set ACR_NAME               --repo $REPO --body $ACR
```

## 5. Day-to-day: deploy by pushing

```powershell
git push origin master
```

GitHub Actions builds the image (tagged with the commit SHA) and runs
`az containerapp update`, which creates a new revision and shifts traffic to it.
Roll back by pointing at an older tag:

```powershell
az containerapp update -n $APP -g $RG --image "$ACR.azurecr.io/planset-qc:<old-sha>"
```

---

## Operations notes

- **Data persistence** — everything under `/home/data` (`PLANSET_DATA_DIR`) is on
  the Azure Files share, so it survives revisions, restarts, and redeploys.
- **SQLite specifics** — opened with `nolock=1` because SMB shares don't support
  SQLite's POSIX file locks. Safe only with a single replica (enforced).
- **Logs** — `az containerapp logs show -n $APP -g $RG --type console --tail 100`
  (live: add `--follow`); the app also writes `/home/data/logs/planset_qc.log`.
- **Cost** — the always-on **2 vCPU / 4 GiB** replica is the main cost (~$45–75/mo);
  plus storage, ACR Basic, and Log Analytics (~$10–15/mo combined); OpenAI is
  usage-based. Levers: drop to **1 vCPU / 2 GiB** (`cpuCores`/`memorySize` in
  `infra/main.parameters.json`) to roughly halve compute, or set `minReplicas: 0`
  to pay nothing while idle at the cost of a ~30–60 s cold start on the first
  request (still SQLite-safe — never more than one replica).
- **Secrets** — `OPENAI_API_KEY` is a Container App secret. Rotate with
  `az containerapp secret set -n $APP -g $RG --secrets openai-api-key=<new>` then
  restart the revision.

## Local development is unchanged

Run the backend (`uvicorn app.main:app --reload`; leaves `FRONTEND_DIST` unset so
the SPA mount is skipped) and the frontend (`npm run dev` on :5173, which targets
the backend on :8000). Copy [backend/.env.example](backend/.env.example) to
`backend/.env` for your local key. See [README.md](README.md).
