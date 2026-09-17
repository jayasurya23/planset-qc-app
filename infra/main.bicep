// Castillo QAQC Automation — Azure infrastructure (Azure Container Apps).
//
// Provisions: Log Analytics, a Storage account + Azure Files share (persistent
// data), a Container Apps managed environment with that share linked, and the
// Container App itself — single always-on replica, pulling its image from an
// existing ACR via a user-assigned managed identity.
//
// The ACR is created and the image is built BEFORE this template is deployed
// (a Container App revision needs its image to exist to start), so the registry
// is referenced here as `existing`. See DEPLOYMENT.md.
//
// Entra (org-only) sign-in is provisioned here as a containerApps/authConfigs
// resource (param enableEntraAuth, default true) so a full redeploy preserves
// the sign-in gate instead of silently dropping it. The Entra *app
// registration* (client id + secret) is still created once out-of-band — see
// DEPLOYMENT.md.
//
// Secrets live in Key Vault, not in this template. The OpenAI key, the Entra
// client secret and the monday.com token are Container App secrets that
// *reference* the vault and resolve through the app's identity, so no
// deployment carries a secret value and a redeploy cannot drop or overwrite
// one. Like the ACR, the vault is created -- and its secrets put in -- before
// this template is deployed, because a revision cannot start while a
// referenced secret is missing. Rotate a secret by adding a new version in the
// vault; see DEPLOYMENT.md.

@description('Globally-unique base name (lowercase letters/numbers/hyphens). Becomes the Container App name and the *.azurecontainerapps.io host.')
param appName string

@description('Azure region. Defaults to the resource group location.')
param location string = resourceGroup().location

@description('vCPU cores per replica (Consumption: memory must be 2x this in Gi).')
param cpuCores string = '2.0'

@description('Memory per replica, e.g. 4.0Gi (must be 2x cpuCores).')
param memorySize string = '4.0Gi'

@description('Container image tag to run. CI overrides this per deploy.')
param imageTag string = 'latest'

@description('Enforce Microsoft Entra (org-only) sign-in via Container Apps built-in auth. Leave true for production.')
param enableEntraAuth bool = true

@description('Entra app-registration (client) id for the sign-in. A public identifier, not a secret.')
param authClientId string = '84813b51-e6a9-48ac-af1e-4db89d6727f7'

@description('Entra tenant id whose org users may sign in (single-tenant).')
param authTenantId string = '551da9d2-5fa9-40e4-a8a4-4845c4b6376a'

@description('Key Vault holding the app secrets: openai-api-key, microsoft-provider-authentication-secret (when enableEntraAuth) and monday-api-token (when enableMondayLinks). Created before this template; see DEPLOYMENT.md.')
param keyVaultName string = 'castillo-qaqc-kv'

@description('Resolve project Project IDs to monday.com boards. Needs monday-api-token in the vault; without it Project IDs and PMO 360 links still work.')
param enableMondayLinks bool = true

@description('Base URL of PMO 360 for Project ID deep links. Empty hides the PMO 360 link.')
param pmo360BaseUrl string = 'https://pmo360.castillope.com'

@description('Custom domain served by the app. Empty for none (e.g. a first deploy, before DNS and the certificate exist; see DEPLOYMENT.md).')
param customDomainName string = 'qc.castillope.com'

@description('Name of the environment managed certificate issued for customDomainName.')
param managedCertificateName string = 'mc-castillo-qaqc--qc-castillope-co-2237'

var acrName = toLower(replace('${appName}acr', '-', ''))
var storageName = toLower('st${uniqueString(resourceGroup().id, appName)}')
var envName = '${appName}-env'
var logName = '${appName}-logs'
var image = 'planset-qc'
var shareName = 'data'
var envStorageName = 'datamount'
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var kvSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

// ACR is created (and the image built) before this deployment.
resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: acrName
}

// Identity the Container App uses to pull from ACR — created first so the
// AcrPull role can be granted before the app tries to pull (avoids a cold-start
// race on a system-assigned identity).
resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${appName}-id'
  location: location
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, uami.id, 'AcrPull')
  scope: acr
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

// The vault is created, and its secrets put in, before this deployment.
resource kv 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

// Read-only access to secret values, for the same identity that pulls images.
resource kvSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(kv.id, uami.id, 'KeyVaultSecretsUser')
  scope: kv
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRoleId)
  }
}

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logName
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

// Persistent data: SQLite, uploaded PDFs, snippets, page images, exports, logs.
resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-01-01' = {
  parent: storage
  name: 'default'
  properties: {
    // Soft delete for the share holding the database and every run: Azure's
    // default for new accounts, stated here so a redeploy cannot turn it off.
    shareDeleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-01-01' = {
  parent: fileService
  name: shareName
  properties: {
    shareQuota: 100
    enabledProtocols: 'SMB'
    accessTier: 'TransactionOptimized'
  }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: envName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: law.properties.customerId
        sharedKey: law.listKeys().primarySharedKey
      }
    }
  }
}

// Make the file share available to apps in the environment.
resource envStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: env
  name: envStorageName
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: shareName
      accessMode: 'ReadWrite'
    }
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uami.id}': {}
    }
  }
  dependsOn: [
    // Ensure the AcrPull grant exists before the app pulls its image, and the
    // vault grant before it resolves its secrets.
    // (The data volume's dependency on envStorage is implicit via its name.)
    acrPull
    kvSecretsUser
  ]
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
        // The certificate is issued against a hostname already added to the
        // app, so it is created out-of-band (DEPLOYMENT.md) and referenced here
        // -- leaving the binding out would remove the domain on redeploy.
        customDomains: empty(customDomainName) ? [] : [
          {
            name: customDomainName
            bindingType: 'SniEnabled'
            certificateId: '${env.id}/managedCertificates/${managedCertificateName}'
          }
        ]
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: uami.id
        }
      ]
      // References into Key Vault, resolved at runtime through the app's
      // identity: the template never holds a value, so a redeploy needs none.
      secrets: concat(
        [
          {
            name: 'openai-api-key'
            keyVaultUrl: '${kv.properties.vaultUri}secrets/openai-api-key'
            identity: uami.id
          }
        ],
        enableEntraAuth ? [
          {
            // Client secret for the Entra built-in auth provider; referenced
            // by the authConfig's clientSecretSettingName below.
            name: 'microsoft-provider-authentication-secret'
            keyVaultUrl: '${kv.properties.vaultUri}secrets/microsoft-provider-authentication-secret'
            identity: uami.id
          }
        ] : [],
        enableMondayLinks ? [
          {
            name: 'monday-api-token'
            keyVaultUrl: '${kv.properties.vaultUri}secrets/monday-api-token'
            identity: uami.id
          }
        ] : []
      )
    }
    template: {
      containers: [
        {
          name: image
          image: '${acr.properties.loginServer}/${image}:${imageTag}'
          resources: {
            cpu: json(cpuCores)
            memory: memorySize
          }
          env: concat([
            { name: 'AI_PROVIDER', value: 'openai' }
            { name: 'OPENAI_MODEL', value: 'gpt-5.4-mini' }
            { name: 'OPENAI_MODEL_DEEP', value: 'gpt-5.4' }
            { name: 'OPENAI_API_KEY', secretRef: 'openai-api-key' }
            // Best-effort determinism, so before/after runs of one planset
            // are comparable (see backend/.env.example). Set on the live app
            // before it was in this template.
            { name: 'OPENAI_SEED', value: '42' }
            { name: 'PLANSET_DATA_DIR', value: '/home/data' }
            { name: 'FRONTEND_DIST', value: '/app/frontend_dist' }
            { name: 'PMO360_BASE_URL', value: pmo360BaseUrl }
          ], enableMondayLinks ? [
            // Only referenced when the secret is declared: a secretRef to a
            // missing secret fails the revision.
            { name: 'MONDAY_API_TOKEN', secretRef: 'monday-api-token' }
          ] : [])
          volumeMounts: [
            {
              volumeName: 'data'
              mountPath: '/home/data'
            }
          ]
        }
      ]
      scale: {
        // SQLite + a single Azure Files writer => exactly one always-on replica.
        minReplicas: 1
        maxReplicas: 1
      }
      volumes: [
        {
          name: 'data'
          storageType: 'AzureFile'
          storageName: envStorage.name
        }
      ]
    }
  }
}

// Built-in Microsoft Entra (org-only) sign-in in front of the whole app.
// Mirrors what `az containerapp auth` configures, but in IaC so a full
// redeploy can't silently drop the sign-in gate. Unauthenticated browsers are
// redirected to the Microsoft login; the backend reads the injected
// X-MS-CLIENT-PRINCIPAL-* identity headers (see backend/app/auth.py).
resource authConfig 'Microsoft.App/containerApps/authConfigs@2024-03-01' = if (enableEntraAuth) {
  parent: app
  name: 'current'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      redirectToProvider: 'azureactivedirectory'
      unauthenticatedClientAction: 'RedirectToLoginPage'
    }
    identityProviders: {
      azureActiveDirectory: {
        registration: {
          clientId: authClientId
          clientSecretSettingName: 'microsoft-provider-authentication-secret'
          // Resolves to https://login.microsoftonline.com/<tenant>/v2.0 on
          // Azure public cloud — matches the live issuer, cloud-portable.
          openIdIssuer: '${environment().authentication.loginEndpoint}${authTenantId}/v2.0'
        }
      }
    }
    login: {
      preserveUrlFragmentsForLogins: false
    }
  }
}

output fqdn string = app.properties.configuration.ingress.fqdn
output appUrl string = 'https://${app.properties.configuration.ingress.fqdn}'
output acrLoginServer string = acr.properties.loginServer
output appName string = app.name
