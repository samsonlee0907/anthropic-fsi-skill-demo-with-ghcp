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

# Build in ACR WITHOUT depending on log streaming. On Windows `az acr build` streams
# server-side logs through colorama and crashes with a cosmetic
# `UnicodeEncodeError: 'charmap' codec can't encode ...` (az.cmd launches `python.exe -I`
# so PYTHONUTF8/console code page can't help). The build itself SUCCEEDS server-side, but
# the crash makes the command exit non-zero BEFORE the image is pushed -- so a naive
# `az acr build | Out-Null` followed by `containerapp create` races the image and fails
# with `MANIFEST_UNKNOWN: manifest tagged by "latest" is not found`. Instead: capture the
# queued run id (printed before any crash), ignore the streaming failure, and poll the run
# status authoritatively. Mirrors Invoke-AcrBuild in deploy.ps1.
Write-Host "Building $ImageTag in registry $RegistryName ..."
$buildOut = az acr build --registry $RegistryName --image $ImageTag $SourceDir 2>&1 | Out-String
$runId = [regex]::Match($buildOut, 'Queued a build with ID:\s*(\S+)').Groups[1].Value
if (-not $runId) {
    Write-Host $buildOut
    throw "az acr build ($ImageTag) failed before a run was queued."
}
Write-Host "  queued ACR run $runId; polling status ..."
$status = ''
for ($i = 0; $i -lt 120; $i++) {
    Start-Sleep -Seconds 5
    $status = az acr task show-run --registry $RegistryName --run-id $runId --query status -o tsv 2>$null
    if ($status -in @('Succeeded', 'Failed', 'Canceled', 'Error', 'Timeout')) { break }
}
if ($status -ne 'Succeeded') {
    throw "ACR build run $runId did not succeed (status: $status). Check: az acr task logs --registry $RegistryName --run-id $runId"
}
Write-Host "  ACR run $runId Succeeded."

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
    if ($LASTEXITCODE -ne 0) { throw "Creating SEC EDGAR Container App $AppName failed." }
} else {
    Write-Host "Updating Container App $AppName ..."
    az containerapp update `
        --name $AppName `
        --resource-group $ResourceGroup `
        --image $image `
        --set-env-vars $envArgs `
        --revision-suffix ("v" + (Get-Date -Format 'MMddHHmmss')) | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Updating SEC EDGAR Container App $AppName failed." }
}

$fqdn = az containerapp show -g $ResourceGroup -n $AppName --query "properties.configuration.ingress.fqdn" -o tsv
if ($LASTEXITCODE -ne 0 -or -not $fqdn) { throw "Could not resolve SEC EDGAR ingress for $AppName." }
$ready = $false
for ($attempt = 1; $attempt -le 12; $attempt++) {
    try {
        $health = Invoke-WebRequest -Uri "https://$fqdn/healthz" -UseBasicParsing -TimeoutSec 10
        if ($health.StatusCode -eq 200 -and $health.Content.Trim() -eq 'ok') { $ready = $true; break }
        Write-Warning "SEC EDGAR liveness returned unexpected content (attempt $attempt/12)."
    } catch {
        Write-Warning "SEC EDGAR liveness request failed (attempt $attempt/12): $($_.Exception.Message)"
    }
    Start-Sleep -Seconds 5
}
if (-not $ready) { throw "SEC EDGAR is not healthy; inspect Container App console logs before registering its connection." }
$mcpUrl = "https://$fqdn/mcp"
Write-Host "SEC EDGAR MCP deployed: $mcpUrl"
return $mcpUrl
