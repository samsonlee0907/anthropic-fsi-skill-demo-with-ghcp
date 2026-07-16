<#
.SYNOPSIS
    Map infra outputs into the azd environment used to deploy the hosted agents.

.DESCRIPTION
    The hosted-agent azure.yaml (agents/hosted/_azd/azure.yaml) resolves ${VAR}
    placeholders from the azd environment. This script sets every one of those
    variables from the bicep outputs, derives the per-scenario toolbox MCP
    endpoints from the project endpoint, and generates a shared SEC MCP secret if
    one was not supplied. It is idempotent and safe to re-run.

.NOTES
    Called by deploy.ps1. Can also be run standalone once you have the infra
    outputs (e.g. from `azd env get-value AZURE_AI_PROJECT_ENDPOINT`).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ProjectEndpoint,
    [Parameter(Mandatory = $true)][string]$StorageBlobEndpoint,
    [string]$ProjectId = '',
    [string]$ModelDeploymentName = 'gpt-5.4',
    [string]$SecEdgarMcpUrl = '',
    [string]$FsiMcpKey = '',
    [string]$EnvName = 'fsi-demo',
    [string]$ResourceGroup = '',
    [string]$SecEdgarAppName = 'ca-secedgar-mcp',
    [string]$AzdDir = (Join-Path $PSScriptRoot '..\agents\hosted\_azd')
)

$ErrorActionPreference = 'Stop'
$ProjectEndpoint = $ProjectEndpoint.TrimEnd('/')
$StorageBlobEndpoint = $StorageBlobEndpoint.TrimEnd('/')
$AzdDir = (Resolve-Path $AzdDir).Path

# --- Toolbox MCP endpoint derivation --------------------------------------
# The FoundryToolbox preview consumes the toolbox over its MCP endpoint. It is
# derived from the project endpoint + toolbox name in ONE place here; adjust this
# single function if the preview MCP path changes.
# NOTE: the `?api-version=v1` query string is REQUIRED — Foundry returns HTTP 400
# without it, and the SDK uses TOOLBOX_ENDPOINT verbatim (only its own fallback
# builder appends api-version). Verified against agent-framework-foundry-hosting
# 1.0.0a260630 _toolbox.py and a live 424/400 startup crash without it.
function Get-ToolboxEndpoint([string]$name) {
    return "$ProjectEndpoint/toolboxes/$name/mcp?api-version=v1"
}

# --- Ensure the azd environment exists ------------------------------------
Push-Location $AzdDir
try {
    $envs = (azd env list --output json 2>$null | ConvertFrom-Json)
    $exists = $false
    foreach ($e in $envs) { if ($e.Name -eq $EnvName) { $exists = $true } }
    if (-not $exists) {
        Write-Host "Creating azd environment '$EnvName' in $AzdDir"
        azd env new $EnvName --no-prompt | Out-Null
    }
    azd env select $EnvName | Out-Null

    # --- SEC EDGAR var resolution (preserve + auto-discover) -----------------
    # Never wipe a previously-good SEC_EDGAR_MCP_URL / FSI_MCP_KEY. If the caller
    # did not pass them (blank), fall back to the value already in the azd env,
    # then — if a resource group is known — auto-discover them from the deployed
    # SEC EDGAR MCP container app. This stops re-runs from silently dropping the
    # SEC toolbox binding (see docs/runbook.md §8 "SEC EDGAR toolbox drift").
    if (-not $SecEdgarMcpUrl -or -not $FsiMcpKey) {
        foreach ($line in @(azd env get-values 2>$null)) {
            if (-not $SecEdgarMcpUrl -and $line -match '^SEC_EDGAR_MCP_URL="?(.+?)"?$') { $SecEdgarMcpUrl = $Matches[1] }
            if (-not $FsiMcpKey -and $line -match '^FSI_MCP_KEY="?(.+?)"?$') { $FsiMcpKey = $Matches[1] }
        }
    }
    if ((-not $SecEdgarMcpUrl -or -not $FsiMcpKey) -and $ResourceGroup) {
        $fqdn = az containerapp show -n $SecEdgarAppName -g $ResourceGroup `
            --query 'properties.configuration.ingress.fqdn' -o tsv 2>$null
        if ($fqdn) {
            if (-not $SecEdgarMcpUrl) { $SecEdgarMcpUrl = "https://$fqdn/mcp" }
            if (-not $FsiMcpKey) {
                $FsiMcpKey = az containerapp show -n $SecEdgarAppName -g $ResourceGroup `
                    --query "properties.template.containers[0].env[?name=='FSI_MCP_KEY']|[0].value" -o tsv 2>$null
            }
            Write-Host "  [auto] discovered SEC EDGAR MCP from '$SecEdgarAppName' in '$ResourceGroup'"
        }
    }

    if (-not $FsiMcpKey -and $SecEdgarMcpUrl) {
        # Generate a 32-byte URL-safe shared secret for the SEC MCP header.
        $bytes = New-Object 'System.Byte[]' 32
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $FsiMcpKey = [Convert]::ToBase64String($bytes).Replace('+', '-').Replace('/', '_').TrimEnd('=')
    }

    $vars = [ordered]@{
        FOUNDRY_PROJECT_ENDPOINT       = $ProjectEndpoint
        AZURE_AI_PROJECT_ID            = $ProjectId
        AZURE_AI_MODEL_DEPLOYMENT_NAME = $ModelDeploymentName
        TOOLBOX_ENDPOINT_EQUITY        = (Get-ToolboxEndpoint 'tb-equity-research')
        TOOLBOX_ENDPOINT_IB            = (Get-ToolboxEndpoint 'tb-ib-pitch')
        TOOLBOX_ENDPOINT_LBO           = (Get-ToolboxEndpoint 'tb-pe-lbo')
        STORAGE_BLOB_ENDPOINT          = $StorageBlobEndpoint
        SEC_EDGAR_MCP_URL              = $SecEdgarMcpUrl
        FSI_MCP_KEY                    = $FsiMcpKey
    }

    foreach ($k in $vars.Keys) {
        $v = $vars[$k]
        if ($null -eq $v) { $v = '' }
        azd env set $k $v | Out-Null
        $shown = if ($k -eq 'FSI_MCP_KEY' -and $v) { '***' } else { $v }
        Write-Host ("  {0,-32} = {1}" -f $k, $shown)
    }
    Write-Host "azd agent environment '$EnvName' updated."

    # Return the (possibly generated) key so the caller can store it in Key Vault.
    return $FsiMcpKey
}
finally {
    Pop-Location
}
