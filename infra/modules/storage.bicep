metadata description = 'Storage account with blob containers for generated artifacts.'

param location string
param tags object
param storageAccountName string
param principalIds array = []

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    supportsHttpsTrafficOnly: true
    accessTier: 'Hot'
    // Data is protected by Entra ID (AAD) RBAC only: shared-key and anonymous blob
    // access are disabled. Public network access stays Enabled so the Foundry hosted
    // agents (managed compute, not VNet-joined) and the Container Apps BFF can reach
    // the account to egress/download artifacts. Disabling public access here breaks
    // hosted-agent artifact upload (AuthorizationFailure) unless private endpoints are
    // added for both the agent compute and the Container Apps environment.
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource artifactsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'artifacts'
  properties: {
    publicAccess: 'None'
  }
}

resource datasetsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'datasets'
  properties: {
    publicAccess: 'None'
  }
}

// Storage Blob Data Contributor for the given principals (portal/API + Foundry MI)
resource blobContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for pid in principalIds: {
  name: guid(storage.id, pid, 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
    principalId: pid
  }
}]

output storageAccountName string = storage.name
output storageBlobEndpoint string = storage.properties.primaryEndpoints.blob
output artifactsContainerName string = artifactsContainer.name
output datasetsContainerName string = datasetsContainer.name
