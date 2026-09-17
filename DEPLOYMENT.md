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
                                                 ├─ Entra built-in auth (org-only)
                                                 └─ secrets ◄─ Key Vault (via the app's identity)
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
| User-assigned identity (ACR pull, Key Vault read) | `castillo-qaqc-automation-id` |
| Key Vault (app secrets) | `castillo-qaqc-kv` |

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
- Your OpenAI API key, and optionally a monday.com API token.

```powershell
$RG  = "castillo-qaqc-automation-rg"
$LOC = "eastus"
$APP = "castillo-qaqc-automation"
$ACR = "castilloqaqcautomationacr"
$KV  = "castillo-qaqc-kv"
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

**Secrets first.** The app's secrets live in Key Vault, and the Container App
only *references* them — so no deployment carries a secret, and a redeploy can
never drop one. A revision cannot start while a referenced secret is missing, so
the vault and its secrets come before the template:

```powershell
az provider register --namespace Microsoft.KeyVault --wait    # once per subscription
az keyvault create -n $KV -g $RG -l $LOC --enable-rbac-authorization true
az role assignment create --assignee "<you@castillope.com>" --role "Key Vault Secrets Officer" `
  --scope (az keyvault show -n $KV --query id -o tsv)
az keyvault secret set --vault-name $KV --name openai-api-key   --value="<OpenAI key>"   --query name -o tsv
az keyvault secret set --vault-name $KV --name monday-api-token --value="<monday token>" --query name -o tsv
```

(Skip the monday line and add `-p enableMondayLinks=false` below if you have no
monday token.) The template grants the app's identity read access to the vault.

The Entra sign-in secret only exists after step 3, so the first deploy runs
without the sign-in gate:

```powershell
az deployment group create -g $RG -f infra/main.bicep -p infra/main.parameters.json `
  -p enableEntraAuth=false
```

This creates Log Analytics, the storage account + `data` file share, the
managed environment (with the share linked), and the Container App (image pulled
via a user-assigned identity, secrets resolved from Key Vault, models set to
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
The sign-in **wiring lives in `infra/main.bicep`** (an `authConfigs` resource, param
`enableEntraAuth`, default `true`), so a full redeploy of the template preserves the
gate instead of silently dropping it. You only create the Entra **app registration**
once, then put its client secret in the vault.

**One-time — create the app registration** (needs the app's FQDN for the redirect URI):

```powershell
$FQDN = az containerapp show -n $APP -g $RG --query properties.configuration.ingress.fqdn -o tsv

$AUTHID = az ad app create --display-name "Castillo QAQC Automation - Auth" `
  --sign-in-audience AzureADMyOrg `
  --web-redirect-uris "https://$FQDN/.auth/login/aad/callback" --query appId -o tsv
az ad sp create --id $AUTHID
$SECRET = az ad app credential reset --id $AUTHID --query password -o tsv
```

`authClientId` / `authTenantId` are already filled into `infra/main.parameters.json`.
Put the secret in the vault, then apply the wiring by **redeploying the
template** — no secret parameters, and never commit a secret:

```powershell
az keyvault secret set --vault-name $KV --name microsoft-provider-authentication-secret `
  --value="$SECRET" --query name -o tsv
az deployment group create -g $RG --template-file infra/main.bicep `
  --parameters infra/main.parameters.json
```

> The old imperative `az containerapp auth microsoft update` / `az containerapp auth
> update` commands do the same thing and are no longer needed now that the
> `authConfigs` resource is in Bicep. Note that the push-to-deploy CI
> (`az containerapp update --image`) only rolls the image and never touches auth.
> If you lose the secret, reset it (`az ad app credential reset --id <authClientId>`),
> put the new value in the vault, and restart the revision (see "Secrets" below).

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
  the Azure Files share, so it survives revisions, restarts, and redeploys. Share
  soft delete (7 days) is set in the template so a redeploy keeps it.
- **Custom domain** — the app is served at **`qc.castillope.com`** (DNS CNAME to
  the app's default host) with an environment managed certificate. The binding is
  in the template (`customDomainName`, `managedCertificateName`), so redeploys
  keep it. On a brand-new environment deploy once with `-p customDomainName=""`,
  bind the domain — which issues the certificate — then redeploy with the new
  certificate's name:

  ```powershell
  az containerapp hostname add  -n $APP -g $RG --hostname qc.castillope.com
  az containerapp hostname bind -n $APP -g $RG --hostname qc.castillope.com `
    --environment "$APP-env" --validation-method CNAME
  az containerapp env certificate list -n "$APP-env" -g $RG --query "[].name" -o tsv
  ```
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
- **Secrets** — the OpenAI key (`openai-api-key`), the Entra client secret
  (`microsoft-provider-authentication-secret`) and the monday token
  (`monday-api-token`) live in Key Vault **`castillo-qaqc-kv`**. The Container App
  holds only references, resolved through its identity, so template redeploys
  take **no secret parameters** and cannot remove a secret. Rotate by adding a new
  version, then restart so the app reads it at once (it also re-reads the vault
  on its own within about 30 minutes):

  ```powershell
  az keyvault secret set --vault-name $KV --name openai-api-key --value="<new key>" --query name -o tsv
  az containerapp revision restart -n $APP -g $RG `
    --revision (az containerapp show -n $APP -g $RG --query properties.latestRevisionName -o tsv)
  ```

  Writing to the vault needs the **Key Vault Secrets Officer** role on it; the
  app's identity has **Key Vault Secrets User** (read-only), granted by the
  template.
- **Project IDs (PMO 360 and monday.com links)** — a project's Castillo
  Project ID (the "Project ID" column on monday's Portfolio board) links to PMO
  360 as `<PMO360_BASE_URL>/portfolio?project_id=<value>`; that needs no
  credentials (`pmo360BaseUrl` in Bicep, default production PMO 360 —
  point a staging app at staging PMO 360). Linking to the project's **monday
  board** needs a monday.com API token: `monday-api-token` in the vault (see
  "Secrets" above; `enableMondayLinks=false` runs without it).

  A restart ends any analysis in progress — change secrets when no runs are
  queued. A personal monday token carries its owner's full permissions; the app
  only sends read queries and refuses mutations, but create the token from an
  account that can read the PMO workspace's Portfolio board and project boards
  and nothing it does not need. Without the token, Project IDs and PMO 360 links
  still work and projects show "monday lookup not configured".

  On the restart that picks the token up, the app links every project whose
  Project ID was saved without it (in the background; see the log line
  "resolved waiting Project IDs at startup"). To re-check all projects later,
  `POST /api/monday/refresh-projects` (add `?include_linked=true` to include
  ones already linked).


## Local development is unchanged

Run the backend (`uvicorn app.main:app --reload`; leaves `FRONTEND_DIST` unset so
the SPA mount is skipped) and the frontend (`npm run dev` on :5173, which targets
the backend on :8000). Copy [backend/.env.example](backend/.env.example) to
`backend/.env` for your local key. See [README.md](README.md).
