metadata description = 'Container Apps environment hosting the FSI portal (frontend) and API (backend).'

param location string
param tags object
param environmentName string
param apiAppName string
param portalAppName string
param logAnalyticsName string
param userAssignedIdentityId string
param registryLoginServer string

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsName
}

@description('Container image for the API. Defaults to a placeholder until the real image is pushed.')
param apiImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
@description('Container image for the portal. Defaults to a placeholder until the real image is pushed.')
param portalImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

param apiEnv array = []
param portalEnv array = []

resource env 'Microsoft.App/managedEnvironments@2026-01-01' = {
  name: environmentName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

resource apiApp 'Microsoft.App/containerApps@2026-01-01' = {
  name: apiAppName
  location: location
  tags: union(tags, { 'azd-service-name': 'api' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${userAssignedIdentityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        corsPolicy: {
          allowedOrigins: ['*']
          allowedMethods: ['GET', 'POST', 'OPTIONS']
          allowedHeaders: ['*']
        }
      }
      registries: [
        {
          server: registryLoginServer
          identity: userAssignedIdentityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: apiImage
          resources: {
            cpu: json('1.0')
            memory: '2.0Gi'
          }
          env: apiEnv
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 3
      }
    }
  }
}

resource portalApp 'Microsoft.App/containerApps@2026-01-01' = {
  name: portalAppName
  location: location
  tags: union(tags, { 'azd-service-name': 'portal' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${userAssignedIdentityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 3000
        transport: 'auto'
      }
      registries: [
        {
          server: registryLoginServer
          identity: userAssignedIdentityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'portal'
          image: portalImage
          resources: {
            cpu: json('0.5')
            memory: '1.0Gi'
          }
          env: portalEnv
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 3
      }
    }
  }
}

output environmentName string = env.name
output apiAppName string = apiApp.name
output apiFqdn string = apiApp.properties.configuration.ingress.fqdn
output portalAppName string = portalApp.name
output portalFqdn string = portalApp.properties.configuration.ingress.fqdn
