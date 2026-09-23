$ErrorActionPreference = 'Stop'
$guard = Join-Path $PSScriptRoot '..\ensure_storage_public.ps1'

function Test-Guard {
    param(
        [string]$Case,
        [bool]$OptOut = $true,
        [string]$Failure = '',
        [bool]$AlreadyTagged = $false
    )
    $state = @{
        calls = [System.Collections.Generic.List[string]]::new()
        failure = $Failure
        account = @{
            id = '/subscriptions/test/resourceGroups/rg-new/providers/Microsoft.Storage/storageAccounts/stnew'
            tags = @{ workload = 'fsi-multiagent-demo' }
            publicNetworkAccess = 'Enabled'
            networkRuleSet = @{ defaultAction = 'Allow' }
            allowBlobPublicAccess = $false
            allowSharedKeyAccess = $false
        }
    }
    if ($AlreadyTagged) { $state.account.tags.SecurityControl = 'Ignore' }
    function az {
        $call = $args -join ' '
        $state.calls.Add($call)
        $global:LASTEXITCODE = 0
        if ($call -like 'tag update*') {
            if ($state.failure -eq 'tag') { $global:LASTEXITCODE = 1; return }
            $state.account.tags.SecurityControl = 'Ignore'
        } elseif ($call -like 'storage account update*') {
            if ($state.failure -eq 'update') { $global:LASTEXITCODE = 1; return }
            if ($state.failure -eq 'policy') { $state.account.publicNetworkAccess = 'Disabled' }
            if ($state.failure -eq 'anonymous') { $state.account.allowBlobPublicAccess = $true }
            if ($state.failure -eq 'shared-key') { $state.account.allowSharedKeyAccess = $true }
            if ($state.failure -eq 'firewall') { $state.account.networkRuleSet.defaultAction = 'Deny' }
        } elseif ($call -like 'storage account show*') {
            if ($state.failure -eq 'read') { $global:LASTEXITCODE = 1; return }
            if ($call -like '*--query publicNetworkAccess*') {
                return $state.account.publicNetworkAccess
            }
            return $state.account | ConvertTo-Json -Depth 5
        } else {
            throw "Unexpected cloud operation: $call"
        }
    }
    $failed = $false
    try { & $guard -ResourceGroup rg-new -StorageAccountName stnew -DemoPolicyOptOut:$OptOut }
    catch { $failed = $true }
    if ($failed -ne [bool]$Failure) { throw "$Case : unexpected failure state $failed" }
    $tagCalls = @($state.calls | Where-Object { $_ -like 'tag update*' })
    if (-not $OptOut -and $tagCalls.Count) { throw "$Case : unapproved tag mutation" }
    if ($AlreadyTagged -and $tagCalls.Count) { throw "$Case : unnecessary tag rewrite" }
    if ($tagCalls.Count -and $tagCalls[0] -notlike '*--resource-id */storageAccounts/stnew --operation Merge*') {
        throw "$Case : exclusion must be merged at storage-account scope"
    }
    if ($state.account.tags.workload -ne 'fsi-multiagent-demo') { throw "$Case : lost existing tags" }
    Write-Output "PASS: $Case"
}

Test-Guard -Case 'default path never applies governance opt-out' -OptOut $false
Test-Guard -Case 'explicit opt-out preserves private authenticated access'
Test-Guard -Case 'existing tag does not need a rewrite' -AlreadyTagged $true
foreach ($failure in @('read', 'tag', 'update', 'policy', 'anonymous', 'shared-key', 'firewall')) {
    Test-Guard -Case "fails closed on $failure" -Failure $failure
}
