<#
.SYNOPSIS
    Build and deploy the self-hosted SEC EDGAR MCP server as a Container App.

.DESCRIPTION
    Optional. Only needed for public-company (SEC EDGAR) grounding. Builds the
    image in agents/mcp/sec-edgar to ACR, then creates/updates a Container App in
    the same Container Apps environment, gated by a shared-secret header. Prints the
    MCP URL (…/mcp) so the caller can wire SEC_EDGAR_MCP_URL into the agent env.

    The upstream sec-edgar-mcp package is AGPL-3.0; review licensing before
    commercial redistribution.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ResourceGroup,
    [Parameter(Mandatory = $true)][string]$RegistryName,
    [Parameter(Mandatory = $true)][string]$UserAssignedIdentityId,
    [Parameter(Mandatory = $true)][string]$SecEdgarUserAgent,
    [Parameter(Mandatory = $true)][string]$FsiMcpKey,
    [string]$AppName = 'ca-secedgar-mcp',
    [string]$KeyHeader = 'x-fsi-mcp-key',
    [string]$ImageTag = 'sec-edgar-mcp:latest',
    [string]$SourceDir = (Join-Path $PSScriptRoot '..\agents\mcp\sec-edgar')
)

$ErrorActionPreference = 'Stop'
$SourceDir = (Resolve-Path $SourceDir).Path

Write-Host "Building $ImageTag in registry $RegistryName ..."
az acr build --registry $RegistryName --image $ImageTag $SourceDir | Out-Null

$image = "$RegistryName.azurecr.io/$ImageTag"

# Discover the Container Apps environment in the resource group.
$caeName = az containerapp env list -g $ResourceGroup --query "[0].name" -o tsv
if (-not $caeName) { throw "No Container Apps environment found in $ResourceGroup." }

$exists = az containerapp show -g $ResourceGroup -n $AppName --query "name" -o tsv 2>$null
$envArgs = @(
    "SEC_EDGAR_USER_AGENT=$SecEdgarUserAgent",
    "FSI_MCP_KEY=$FsiMcpKey",
    "FSI_MCP_KEY_HEADER=$KeyHeader"
)

if (-not $exists) {
    Write-Host "Creating Container App $AppName ..."
    az containerapp create `
        --name $AppName `
        --resource-group $ResourceGroup `
        --environment $caeName `
        --image $image `
        --target-port 8080 `
        --ingress external `
        --min-replicas 1 `
        --max-replicas 2 `
        --registry-server "$RegistryName.azurecr.io" `
        --registry-identity $UserAssignedIdentityId `
        --user-assigned $UserAssignedIdentityId `
        --env-vars $envArgs | Out-Null
} else {
    Write-Host "Updating Container App $AppName ..."
    az containerapp update `
        --name $AppName `
        --resource-group $ResourceGroup `
        --image $image `
        --set-env-vars $envArgs `
        --revision-suffix ("v" + (Get-Date -Format 'MMddHHmmss')) | Out-Null
}

$fqdn = az containerapp show -g $ResourceGroup -n $AppName --query "properties.configuration.ingress.fqdn" -o tsv
$mcpUrl = "https://$fqdn/mcp"
Write-Host "SEC EDGAR MCP deployed: $mcpUrl"
return $mcpUrl
