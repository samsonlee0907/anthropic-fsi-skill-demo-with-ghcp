<#
.SYNOPSIS
    Grant each deployed hosted agent's instance identity the RBAC it needs.

.DESCRIPTION
    A Foundry hosted agent authenticates as its per-agent *instance identity*. That
    identity needs `Cognitive Services User` on the Foundry account (to call the
    model/tools) and `Storage Blob Data Contributor` on the storage account (so the
    ArtifactEgressMiddleware can upload generated files). These identities only
    exist after the agents are deployed, so this runs as a post-deploy step.

    Pass the instance principal ids explicitly with -PrincipalIds, or let the script
    discover user-assigned identities in the resource group. Role assignments are
    idempotent; re-running is safe.

.NOTES
    To find an agent's principal id if discovery misses it, log the storage-token
    `oid` from the container and pass it here (see docs/runbook.md, RBAC section).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ResourceGroup,
    [Parameter(Mandatory = $true)][string]$AiAccountName,
    [Parameter(Mandatory = $true)][string]$StorageAccountName,
    [string[]]$PrincipalIds = @()
)

$ErrorActionPreference = 'Stop'

$subId = (az account show --query id -o tsv)
$aiScope = "/subscriptions/$subId/resourceGroups/$ResourceGroup/providers/Microsoft.CognitiveServices/accounts/$AiAccountName"
$stScope = "/subscriptions/$subId/resourceGroups/$ResourceGroup/providers/Microsoft.Storage/storageAccounts/$StorageAccountName"

if (-not $PrincipalIds -or $PrincipalIds.Count -eq 0) {
    Write-Host "No -PrincipalIds supplied; discovering user-assigned identities in $ResourceGroup ..."
    $ids = az identity list -g $ResourceGroup --query "[].principalId" -o tsv
    $PrincipalIds = @($ids | Where-Object { $_ })
}

if (-not $PrincipalIds -or $PrincipalIds.Count -eq 0) {
    Write-Warning "No agent instance principal ids found. Grant RBAC manually once the agents are deployed (see docs/runbook.md)."
    return
}

function Grant([string]$principalId, [string]$role, [string]$scope) {
    # Idempotent: 'az role assignment create' returns the existing assignment if present.
    az role assignment create `
        --assignee-object-id $principalId `
        --assignee-principal-type ServicePrincipal `
        --role $role `
        --scope $scope 1>$null 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host ("  [ok]   {0} -> {1}" -f $role, $principalId)
    } else {
        Write-Host ("  [skip] {0} -> {1} (may already exist)" -f $role, $principalId)
    }
}

foreach ($oid in ($PrincipalIds | Select-Object -Unique)) {
    Write-Host "Granting roles to principal $oid"
    Grant $oid 'Cognitive Services User' $aiScope
    Grant $oid 'Storage Blob Data Contributor' $stScope
}

Write-Host "Agent RBAC grants complete."
