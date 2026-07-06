metadata description = 'Grants a principal data-plane access to the Foundry account for invoking agents.'

param aiAccountName string
param principalId string
param principalType string = 'ServicePrincipal'

resource account 'Microsoft.CognitiveServices/accounts@2026-05-01' existing = {
  name: aiAccountName
}

// Azure AI User — call Agent Service / inference on the project
resource aiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, principalId, '53ca6127-db72-4b80-b1b0-d745d6d5456d')
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '53ca6127-db72-4b80-b1b0-d745d6d5456d')
    principalId: principalId
    principalType: principalType
  }
}

// Cognitive Services User — data-plane inference fallback
resource cogUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, principalId, 'a97b65f3-24c7-4388-baec-2e87135dc908')
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'a97b65f3-24c7-4388-baec-2e87135dc908')
    principalId: principalId
    principalType: principalType
  }
}
