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
// Entra (org-only) sign-in is configured out-of-band after deploy via
// `az containerapp auth` (needs the app's FQDN for the redirect URI).

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

@secure()
@description('OpenAI API key — stored as a Container App secret, never in source/state.')
param openAiApiKey string

var acrName = toLower(replace('${appName}acr', '-', ''))
var storageName = toLower('st${uniqueString(resourceGroup().id, appName)}')
var envName = '${appName}-env'
var logName = '${appName}-logs'
var image = 'planset-qc'
var shareName = 'data'
var envStorageName = 'datamount'
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

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
}

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-01-01' = {
  parent: fileService
  name: shareName
  properties: {
    shareQuota: 100
    enabledProtocols: 'SMB'
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
    // Ensure the AcrPull grant exists before the app pulls its image.
    // (The data volume's dependency on envStorage is implicit via its name.)
    acrPull
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
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: uami.id
        }
      ]
      secrets: [
        {
          name: 'openai-api-key'
          value: openAiApiKey
        }
      ]
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
          env: [
            { name: 'AI_PROVIDER', value: 'openai' }
            { name: 'OPENAI_MODEL', value: 'gpt-5.4-mini' }
            { name: 'OPENAI_MODEL_DEEP', value: 'gpt-5.4' }
            { name: 'OPENAI_API_KEY', secretRef: 'openai-api-key' }
            { name: 'PLANSET_DATA_DIR', value: '/home/data' }
            { name: 'FRONTEND_DIST', value: '/app/frontend_dist' }
          ]
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

output fqdn string = app.properties.configuration.ingress.fqdn
output appUrl string = 'https://${app.properties.configuration.ingress.fqdn}'
output acrLoginServer string = acr.properties.loginServer
output appName string = app.name
