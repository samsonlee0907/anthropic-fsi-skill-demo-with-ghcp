$ErrorActionPreference = 'Stop'
$mapper = Join-Path $PSScriptRoot '..\set_azd_env_from_infra.ps1'
$azdDir = Join-Path $PSScriptRoot '..\..\agents\hosted\_azd'

function Test-Mapping {
    param([string]$Case, [bool]$ExistingSec, [bool]$RejectWrite = $false)
    $state = @{
        written = @{}
        reads = [System.Collections.Generic.List[string]]::new()
        existingSec = $ExistingSec
        rejectWrite = $RejectWrite
    }
    function azd {
        $global:LASTEXITCODE = 0
        switch ($args[1]) {
            'list' { return '[{"Name":"unit-test"}]' }
            'select' { return }
            'get-values' {
                if ($state.existingSec) {
                    return @('SEC_EDGAR_MCP_URL="https://existing.example/mcp"', 'FSI_MCP_KEY="test-existing-key"')
                }
                return @('SEC_EDGAR_MCP_URL=""', 'FSI_MCP_KEY=""')
            }
            'set' {
                if ($state.rejectWrite) { $global:LASTEXITCODE = 1; return }
                $state.written[$args[2]] = $args[3]
            }
            default { throw "Unexpected azd operation: $($args[1])" }
        }
    }
    function az {
        $global:LASTEXITCODE = 0
        $call = $args -join ' '
        $state.reads.Add($call)
        if ($call.Contains('|')) { throw 'JMESPath pipes are not safe through az.cmd on Windows.' }
        if ($call -like '*ingress.fqdn*') { return 'new.example' }
        if ($call -like '*FSI_MCP_KEY*') { return 'test-discovered-key' }
        throw "Unexpected cloud lookup: $call"
    }
    $failed = $false
    try {
        & $mapper -AzdDir $azdDir -EnvName unit-test -ResourceGroup rg-new `
            -ProjectEndpoint 'https://foundry.example/api/projects/test' `
            -ProjectId '/subscriptions/test/resourceGroups/rg-new/providers/Microsoft.CognitiveServices/accounts/test/projects/test' `
            -StorageBlobEndpoint 'https://test.blob.core.windows.net/' `
            -ModelDeploymentName gpt-6-astra | Out-Null
    } catch { $failed = $true }
    if ($failed -ne $RejectWrite) { throw "$Case : unexpected failure state $failed" }
    if (-not $RejectWrite) {
        $expectedUrl = if ($ExistingSec) { 'https://existing.example/mcp' } else { 'https://new.example/mcp' }
        $expectedKey = if ($ExistingSec) { 'test-existing-key' } else { 'test-discovered-key' }
        if ($state.written.SEC_EDGAR_MCP_URL -ne $expectedUrl) { throw "$Case : incorrect SEC URL" }
        if ($state.written.FSI_MCP_KEY -ne $expectedKey) { throw "$Case : incorrect SEC credential" }
        if ($state.written.AZURE_AI_MODEL_DEPLOYMENT_NAME -ne 'gpt-6-astra') { throw "$Case : model mismatch" }
        if ($state.written.AZURE_AI_PROJECT_ENDPOINT -ne $state.written.FOUNDRY_PROJECT_ENDPOINT) {
            throw "$Case : Foundry endpoint aliases must agree"
        }
        if ($ExistingSec -and $state.reads.Count) { throw "$Case : overwrote an existing SEC binding" }
    }
    Write-Output "PASS: $Case"
}

Test-Mapping -Case 'quoted empty values trigger SEC autodiscovery' -ExistingSec $false
Test-Mapping -Case 'existing SEC connection remains unchanged' -ExistingSec $true
Test-Mapping -Case 'failed azd writes abort mapping' -ExistingSec $true -RejectWrite $true
