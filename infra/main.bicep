metadata description = 'FSI Multi-Agent Demo — subscription-scoped deployment: resource group + Foundry + supporting services + Container Apps.'

targetScope = 'subscription'

@minLength(3)
@maxLength(20)
@description('Short environment/app name; drives resource naming and the resource group name.')
param environmentName string = 'fsi-demo'

@description('Azure region for all resources.')
param location string = 'eastus2'

@description('Object ID of a user/group to also grant Foundry data-plane access (optional, for portal testing in ai.azure.com).')
param developerPrincipalId string = ''

@description('Model deployment name the hosted agents run on. MUST match one of the names in modelDeployments (below), or the agents will call a deployment that does not exist.')
param agentModelDeploymentName string = 'gpt-5.4'

@description('Capacity (thousands of tokens/min) for the default gpt-5.4 deployment. Lower it if you are quota-constrained -- gpt-5.4 GlobalStandard quota is subscription-GLOBAL (shared across every region), so switching region does NOT free quota. Ignored when modelDeployments is overridden directly.')
param modelCapacity int = 150

@description('Model deployments to create on the Foundry account. Override to add models or change capacity/region to fit your quota. Keep the agent model (agentModelDeploymentName) in this list.')
param modelDeployments array = [
  {
    name: 'gpt-5.4'
    model: 'gpt-5.4'
    version: '2026-03-05'
    sku: 'GlobalStandard'
    capacity: modelCapacity
  }
]

var resourceGroupName = 'rg-${environmentName}'
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var tags = {
  'azd-env-name': environmentName
  workload: 'fsi-multiagent-demo'
}

// Sanitized token for names that disallow hyphens
var shortToken = substring(resourceToken, 0, 8)

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

// User-assigned identity used by the portal + API container apps
module identity 'modules/identity.bicep' = {
  scope: rg
  name: 'identity'
  params: {
    location: location
    tags: tags
    identityName: 'id-${environmentName}-${shortToken}'
  }
}

module monitoring 'modules/monitoring.bicep' = {
  scope: rg
  name: 'monitoring'
  params: {
    location: location
    tags: tags
    logAnalyticsName: 'log-${environmentName}-${shortToken}'
    appInsightsName: 'appi-${environmentName}-${shortToken}'
  }
}

module foundry 'modules/foundry.bicep' = {
  scope: rg
  name: 'foundry'
  params: {
    location: location
    tags: tags
    aiAccountName: 'aif${shortToken}'
    projectName: 'proj-${environmentName}'
    modelDeployments: modelDeployments
  }
}

module registry 'modules/registry.bicep' = {
  scope: rg
  name: 'registry'
  params: {
    location: location
    tags: tags
    registryName: 'acr${shortToken}'
    principalIds: [identity.outputs.principalId]
  }
}

module storage 'modules/storage.bicep' = {
  scope: rg
  name: 'storage'
  params: {
    location: location
    tags: tags
    storageAccountName: 'st${shortToken}'
    principalIds: [identity.outputs.principalId, foundry.outputs.projectPrincipalId]
  }
}

module keyvault 'modules/keyvault.bicep' = {
  scope: rg
  name: 'keyvault'
  params: {
    location: location
    tags: tags
    keyVaultName: 'kv-${environmentName}-${shortToken}'
    principalIds: [identity.outputs.principalId]
  }
}

// Grant the container-app identity data-plane access to the Foundry account
module foundryRbacApp 'modules/foundry-rbac.bicep' = {
  scope: rg
  name: 'foundry-rbac-app'
  params: {
    aiAccountName: foundry.outputs.aiAccountName
    principalId: identity.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

// Optionally grant a developer/user data-plane access
module foundryRbacDev 'modules/foundry-rbac.bicep' = if (!empty(developerPrincipalId)) {
  scope: rg
  name: 'foundry-rbac-dev'
  params: {
    aiAccountName: foundry.outputs.aiAccountName
    principalId: developerPrincipalId
    principalType: 'User'
  }
}

module containerApps 'modules/containerapps.bicep' = {
  scope: rg
  name: 'container-apps'
  params: {
    location: location
    tags: tags
    environmentName: 'cae-${environmentName}-${shortToken}'
    apiAppName: 'ca-api-${environmentName}'
    portalAppName: 'ca-portal-${environmentName}'
    logAnalyticsName: 'log-${environmentName}-${shortToken}'
    userAssignedIdentityId: identity.outputs.resourceId
    registryLoginServer: registry.outputs.registryLoginServer
    apiEnv: [
      { name: 'PROJECT_ENDPOINT', value: foundry.outputs.projectEndpoint }
      { name: 'STORAGE_BLOB_ENDPOINT', value: storage.outputs.storageBlobEndpoint }
      { name: 'ARTIFACTS_CONTAINER', value: 'artifacts' }
      { name: 'AZURE_CLIENT_ID', value: identity.outputs.clientId }
      { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: monitoring.outputs.appInsightsConnectionString }
    ]
  }
}

output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_LOCATION string = location
output AZURE_AI_ACCOUNT_NAME string = foundry.outputs.aiAccountName
output AZURE_AI_PROJECT_NAME string = foundry.outputs.projectName
output AZURE_AI_PROJECT_ID string = foundry.outputs.projectId
output AZURE_AI_PROJECT_ENDPOINT string = foundry.outputs.projectEndpoint
output AZURE_AI_ACCOUNT_ENDPOINT string = foundry.outputs.accountEndpoint
output AZURE_AI_MODEL_DEPLOYMENT_NAME string = agentModelDeploymentName
output AZURE_CONTAINER_REGISTRY_NAME string = registry.outputs.registryName
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = registry.outputs.registryLoginServer
output AZURE_STORAGE_ACCOUNT string = storage.outputs.storageAccountName
output AZURE_STORAGE_BLOB_ENDPOINT string = storage.outputs.storageBlobEndpoint
output AZURE_KEY_VAULT_NAME string = keyvault.outputs.keyVaultName
output AZURE_KEY_VAULT_URI string = keyvault.outputs.keyVaultUri
output APPLICATIONINSIGHTS_CONNECTION_STRING string = monitoring.outputs.appInsightsConnectionString
output AZURE_CLIENT_ID string = identity.outputs.clientId
output AZURE_MANAGED_IDENTITY_ID string = identity.outputs.resourceId
output API_FQDN string = containerApps.outputs.apiFqdn
output PORTAL_FQDN string = containerApps.outputs.portalFqdn
output PORTAL_URL string = 'https://${containerApps.outputs.portalFqdn}'
output API_URL string = 'https://${containerApps.outputs.apiFqdn}'
