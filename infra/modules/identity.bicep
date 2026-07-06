metadata description = 'User-assigned managed identity for the portal and API container apps.'

param location string
param tags object
param identityName string

resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: tags
}

output resourceId string = uami.id
output clientId string = uami.properties.clientId
output principalId string = uami.properties.principalId
output name string = uami.name
