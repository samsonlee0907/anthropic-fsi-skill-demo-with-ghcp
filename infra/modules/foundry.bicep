metadata description = 'Azure AI Foundry account (AI Services) + project + serialized model deployments for the FSI multi-agent demo.'

param location string
param tags object
param aiAccountName string
param projectName string
param projectDisplayName string = 'FSI Multi-Agent Demo'

@description('Model deployments to create on the account. Deployed one at a time to avoid concurrent-write conflicts.')
param modelDeployments array = [
  {
    name: 'gpt-5.1'
    model: 'gpt-5.1'
    version: '2025-11-13'
    sku: 'GlobalStandard'
    capacity: 150
  }
  {
    name: 'gpt-5.4-mini'
    model: 'gpt-5.4-mini'
    version: '2026-03-17'
    sku: 'GlobalStandard'
    capacity: 150
  }
  {
    name: 'text-embedding-3-large'
    model: 'text-embedding-3-large'
    version: '1'
    sku: 'Standard'
    capacity: 50
  }
]

resource account 'Microsoft.CognitiveServices/accounts@2026-05-01' = {
  name: aiAccountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: aiAccountName
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: false
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2026-05-01' = {
  parent: account
  name: projectName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: projectDisplayName
    description: 'Multi-agent FSI demo: equity research, IB pitch prep, and PE LBO screening.'
  }
}

@batchSize(1)
resource deployments 'Microsoft.CognitiveServices/accounts/deployments@2026-05-01' = [for d in modelDeployments: {
  parent: account
  name: d.name
  sku: {
    name: d.sku
    capacity: d.capacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: d.model
      version: d.version
    }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
  }
}]

output aiAccountName string = account.name
output aiAccountId string = account.id
output aiAccountPrincipalId string = account.identity.principalId
output projectName string = project.name
output projectPrincipalId string = project.identity.principalId
// Foundry Agent Service / project data-plane endpoint
output projectEndpoint string = 'https://${account.name}.services.ai.azure.com/api/projects/${project.name}'
output accountEndpoint string = account.properties.endpoint
