<#
.SYNOPSIS
    Ensure a storage account's publicNetworkAccess is Enabled -- and STAYS Enabled --
    self-healing around an Azure Policy 'modify'/'deny' effect that reverts the setting.

.DESCRIPTION
    The Foundry hosted-agent managed compute and the VNet-less Container Apps BFF reach
    Blob Storage over the public endpoint, gated by Entra ID (AAD) RBAC only
    (allowSharedKeyAccess=false, allowBlobPublicAccess=false). If publicNetworkAccess is
    Disabled with no private endpoints for BOTH, artifact upload/download fails with
    AuthorizationFailure -- the SAME wording as a missing RBAC role, so it is easy to
    misdiagnose.

    Some governed subscriptions attach a management-group Azure Policy with a 'modify'
    effect that forces publicNetworkAccess=Disabled on every write. Under such a policy
    `az storage account update --public-network-access Enabled` SILENTLY NO-OPS: the
    write returns success but the value stays Disabled. So this script never trusts the
    exit code -- it re-reads the actual value, and if the policy is reverting it, creates
    a resource-group-scoped Waiver policy exemption for the offending assignment(s) and
    retries.

    In a clean subscription with no such policy this is a no-op after the first update.

    RBAC: creating a policy exemption needs Microsoft.Authorization/policyExemptions/write
    at the resource group (e.g. Owner or Resource Policy Contributor). If the caller lacks
    it, the script fails fast with actionable guidance instead of leaving a silently
    broken stack.

.PARAMETER PolicyAssignmentId
    Optional. Skip discovery and exempt this specific assignment id. Useful if your
    governance policy is not auto-discovered (e.g. its rule references publicNetworkAccess
    indirectly). Combine with -PolicyDefinitionReferenceIds for initiatives.

.PARAMETER PolicyDefinitionReferenceIds
    Optional. For an initiative (policy set), the definition reference id(s) within it to
    exempt. Ignored for single-definition assignments.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ResourceGroup,
    [Parameter(Mandatory = $true)][string]$StorageAccountName,
    [string]$ExemptionName = 'fsi-storage-public-network-waiver',
    [string]$PolicyAssignmentId = '',
    [string[]]$PolicyDefinitionReferenceIds = @()
)

$ErrorActionPreference = 'Stop'

function Get-Pna {
    az storage account show -n $StorageAccountName -g $ResourceGroup --query publicNetworkAccess -o tsv 2>$null
}

function Set-Enabled {
    az storage account update -n $StorageAccountName -g $ResourceGroup `
        --public-network-access Enabled --default-action Allow --bypass AzureServices 1>$null 2>$null
}

# `az policy [set-]definition show --ids` does NOT work for management-group-scoped
# definitions -- those require --name + --management-group. These resolvers parse the
# scope out of the id and call the correct form, so discovery works whether the policy
# is built-in, subscription-scoped, or (as in governed tenants) management-group-scoped.
function Resolve-PolicyDef {
    param([string]$Id, [switch]$IsSet)
    $kind = if ($IsSet) { 'set-definition' } else { 'definition' }
    if ($Id -match '/managementGroups/([^/]+)/.*/(policySetDefinitions|policyDefinitions)/([^/]+)$') {
        $mg = $Matches[1]; $name = $Matches[3]
        return az policy $kind show --name $name --management-group $mg -o json 2>$null | ConvertFrom-Json
    }
    return az policy $kind show --ids $Id -o json 2>$null | ConvertFrom-Json
}

# --- Attempt 1: plain enable + verify (covers clean subs and transient states) ---------
if ((Get-Pna) -ne 'Enabled') {
    Set-Enabled
    Start-Sleep -Seconds 3
}
if ((Get-Pna) -eq 'Enabled') {
    Write-Host "  [storage] publicNetworkAccess=Enabled"
    return
}

Write-Host "  [storage] still Disabled after update -> an Azure Policy 'modify' effect is reverting it; creating a scoped Waiver exemption ..." -ForegroundColor Yellow

$acctId = az storage account show -n $StorageAccountName -g $ResourceGroup --query id -o tsv 2>$null

# --- Discover the offending assignment(s) ----------------------------------------------
# A modify policy makes the resource *compliant* (it enforced the value), so it does NOT
# show up as NonCompliant. Instead, enumerate the policy assignments effective on the
# account and keep the ones whose (set-)definition governs storage `publicNetworkAccess`.
# Exempting with category=Waiver is harmless for audit-only policies, so we err toward
# exempting any publicNetworkAccess-on-storage definition rather than guessing the effect
# (which is often an assignment parameter and not readable from the definition alone).
$targets = @()  # each: @{ AssignmentId; RefIds = @() }

if ($PolicyAssignmentId) {
    $targets += , @{ AssignmentId = $PolicyAssignmentId; RefIds = @($PolicyDefinitionReferenceIds | Where-Object { $_ }) }
}
else {
    $assignments = az policy assignment list --disable-scope-strict-match --scope $acctId -o json 2>$null | ConvertFrom-Json
    foreach ($a in @($assignments)) {
        $defId = $a.policyDefinitionId
        if (-not $defId) { continue }
        $refIds = @()
        $matched = $false
        if ($defId -match '/policySetDefinitions/') {
            $set = Resolve-PolicyDef -Id $defId -IsSet
            foreach ($pd in @($set.policyDefinitions)) {
                $inner = Resolve-PolicyDef -Id $pd.policyDefinitionId
                $ruleJson = ($inner.policyRule | ConvertTo-Json -Depth 40 -Compress)
                if ($ruleJson -match 'publicNetworkAccess' -and $ruleJson -match 'storageAccounts') {
                    $refIds += $pd.policyDefinitionReferenceId
                    $matched = $true
                }
            }
        }
        else {
            $inner = Resolve-PolicyDef -Id $defId
            $ruleJson = ($inner.policyRule | ConvertTo-Json -Depth 40 -Compress)
            if ($ruleJson -match 'publicNetworkAccess' -and $ruleJson -match 'storageAccounts') {
                $matched = $true  # single-definition assignment -> whole-assignment exemption
            }
        }
        if ($matched) { $targets += , @{ AssignmentId = $a.id; RefIds = @($refIds | Where-Object { $_ }) } }
    }
}

if ($targets.Count -eq 0) {
    throw @"
storage publicNetworkAccess is Disabled and being reverted, but no governing policy that
targets storage 'publicNetworkAccess' was auto-discovered. Re-run this script with the
offending assignment explicitly, e.g.:
  scripts/ensure_storage_public.ps1 -ResourceGroup $ResourceGroup -StorageAccountName $StorageAccountName ``
    -PolicyAssignmentId <assignment-id> -PolicyDefinitionReferenceIds <ref-id>
Find candidates with:  az policy assignment list --disable-scope-strict-match --scope <storage-id>
See docs/runbook.md ('Storage public network access') for the exemption + private-endpoint options.
"@
}

# --- Create the Waiver exemption(s) ----------------------------------------------------
$i = 0
foreach ($t in $targets) {
    $exName = if ($targets.Count -gt 1) { "$ExemptionName-$i" } else { $ExemptionName }
    $i++
    $exArgs = @(
        'policy', 'exemption', 'create',
        '--name', $exName,
        '--resource-group', $ResourceGroup,
        '--policy-assignment', $t.AssignmentId,
        '--exemption-category', 'Waiver',
        '--display-name', 'FSI demo: allow storage public network access',
        '--description', 'Waiver stops a Policy modify effect from re-disabling publicNetworkAccess on demo storage, which otherwise breaks artifact upload/download. Blob access stays gated by Entra ID RBAC (allowSharedKeyAccess=false).'
    )
    if ($t.RefIds.Count -gt 0) { $exArgs += @('--policy-definition-reference-ids') + $t.RefIds }
    az @exArgs 1>$null 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  [storage] created Waiver exemption on $($t.AssignmentId)"
    }
    else {
        Write-Host "  [storage] could not create exemption on $($t.AssignmentId) (may already exist or you lack policyExemptions/write)" -ForegroundColor DarkYellow
    }
}

# --- Attempt 2: re-enable after the exemption propagates + verify ----------------------
Start-Sleep -Seconds 30
Set-Enabled
Start-Sleep -Seconds 5
$final = Get-Pna
if ($final -eq 'Enabled') {
    Write-Host "  [storage] publicNetworkAccess=Enabled (held after exemption)"
    return
}

throw @"
storage publicNetworkAccess is still '$final' after creating a Waiver exemption. Most
likely you lack permission to create policy exemptions (need Owner or Resource Policy
Contributor on $ResourceGroup), or the exemption has not propagated yet.
Remediation options (see docs/runbook.md > 'Storage public network access'):
  1. Ask a Policy owner to add an exemption/exclusion for this resource group, then re-run
     with -SkipInfra -SkipSkills -SkipSecEdgar -SkipAgents (re-runs steps 7b+ only), or
  2. Adopt the private-endpoint architecture (Blob private endpoint + VNet-integrated
     Container Apps and Foundry agent-compute network injection) so storage can stay
     Disabled and satisfy the policy natively.
"@
