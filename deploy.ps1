<#
.SYNOPSIS
    One-command deployment of the FSI multi-agent stack to Azure AI Foundry.

.DESCRIPTION
    Runs the full ordered flow, idempotently, so anyone can reproduce the stack in
    their own subscription:

      1. Provision infra (subscription-scoped bicep): RG, Foundry account/project +
         model deployments, ACR, Storage, Key Vault, App Insights, Container Apps,
         app-identity RBAC.
      2. Register the Anthropic skills as Foundry skills and create the 3 scenario
         toolboxes.
      3. (optional) Deploy the SEC EDGAR MCP Container App and generate its secret.
      4. Bind skills (+ SEC tool) to the toolboxes and promote the default version.
      5. Map infra outputs into the azd agent environment.
      6. Deploy the 3 hosted agents (azd).
      7. Grant each agent's instance identity the RBAC it needs.
      8. Build + deploy the API and portal container images.
      9. Validate all three scenarios end-to-end.

    Re-run safely; use the -Skip* switches to resume after a failure.

.EXAMPLE
    ./deploy.ps1 -EnvName fsi-demo -Location eastus2 `
        -SecEdgarUserAgent "Jane Doe (jane@example.com)"
#>
[CmdletBinding()]
param(
    [string]$EnvName = 'fsi-demo',
    [string]$Location = 'eastus2',
    [string]$SubscriptionId = '',
    [string]$PrincipalId = '',
    [string]$ModelDeploymentName = 'gpt-5.1',
    [string]$SecEdgarUserAgent = '',
    [switch]$SkipInfra,
    [switch]$SkipSkills,
    [switch]$SkipSecEdgar,
    [switch]$SkipAgents,
    [switch]$SkipApps,
    [switch]$SkipValidate
)

$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$azdDir = Join-Path $repo 'agents\hosted\_azd'
$hostedDir = Join-Path $repo 'agents\hosted'

function Require-Tool([string]$name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        throw "Required tool '$name' not found on PATH."
    }
}

Write-Host "== Preflight ==" -ForegroundColor Cyan
'az', 'azd', 'python', 'gh' | ForEach-Object { Require-Tool $_ }
if ($SubscriptionId) { az account set --subscription $SubscriptionId | Out-Null }
if (-not $PrincipalId) { $PrincipalId = az ad signed-in-user show --query id -o tsv }
az config set auth.useAzCliAuth true 2>$null | Out-Null

# ---------------------------------------------------------------------------
# 1. Infra
# ---------------------------------------------------------------------------
if (-not $SkipInfra) {
    Write-Host "== 1. Provisioning infra ($EnvName / $Location) ==" -ForegroundColor Cyan
    $dep = az deployment sub create `
        --name "fsi-$EnvName" `
        --location $Location `
        --template-file (Join-Path $repo 'infra\main.bicep') `
        --parameters environmentName=$EnvName location=$Location `
                     developerPrincipalId=$PrincipalId `
                     agentModelDeploymentName=$ModelDeploymentName `
        -o json | ConvertFrom-Json
} else {
    Write-Host "== 1. Reading existing infra outputs ==" -ForegroundColor Cyan
    $dep = az deployment sub show --name "fsi-$EnvName" -o json | ConvertFrom-Json
}

$o = $dep.properties.outputs
$projectEndpoint  = $o.AZURE_AI_PROJECT_ENDPOINT.value
$storageBlob      = $o.AZURE_STORAGE_BLOB_ENDPOINT.value
$rg               = $o.AZURE_RESOURCE_GROUP.value
$acrName          = $o.AZURE_CONTAINER_REGISTRY_NAME.value
$aiAccount        = $o.AZURE_AI_ACCOUNT_NAME.value
$storageAccount   = $o.AZURE_STORAGE_ACCOUNT.value
$managedIdId      = $o.AZURE_MANAGED_IDENTITY_ID.value
$apiUrl           = $o.API_URL.value
$portalUrl        = $o.PORTAL_URL.value
$env:PROJECT_ENDPOINT = $projectEndpoint
Write-Host "  project=$projectEndpoint"
Write-Host "  rg=$rg acr=$acrName api=$apiUrl"

# ---------------------------------------------------------------------------
# 2. Register skills + create toolboxes
# ---------------------------------------------------------------------------
if (-not $SkipSkills) {
    Write-Host "== 2. Registering skills + toolboxes ==" -ForegroundColor Cyan
    python (Join-Path $repo 'agents\scripts\provision_skills.py')
    python (Join-Path $repo 'agents\scripts\create_toolboxes.py')
}

# ---------------------------------------------------------------------------
# 3. (optional) SEC EDGAR MCP
# ---------------------------------------------------------------------------
$secMcpUrl = ''
$fsiMcpKey = ''
if ($SecEdgarUserAgent -and -not $SkipSecEdgar) {
    Write-Host "== 3. Deploying SEC EDGAR MCP ==" -ForegroundColor Cyan
    # Generate the shared secret up front so both the MCP app and the agents use it.
    $bytes = New-Object 'System.Byte[]' 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $fsiMcpKey = [Convert]::ToBase64String($bytes).Replace('+', '-').Replace('/', '_').TrimEnd('=')
    $secMcpUrl = & (Join-Path $repo 'scripts\deploy_sec_edgar.ps1') `
        -ResourceGroup $rg -RegistryName $acrName -UserAssignedIdentityId $managedIdId `
        -SecEdgarUserAgent $SecEdgarUserAgent -FsiMcpKey $fsiMcpKey | Select-Object -Last 1
    $env:SEC_EDGAR_MCP_URL = $secMcpUrl
    $env:FSI_MCP_KEY = $fsiMcpKey
} else {
    Write-Host "== 3. Skipping SEC EDGAR MCP (no -SecEdgarUserAgent) ==" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------------
# 4. Bind skills (+ SEC tool) to toolboxes and promote the default version
# ---------------------------------------------------------------------------
if (-not $SkipSkills) {
    Write-Host "== 4. Binding skills to toolboxes ==" -ForegroundColor Cyan
    python (Join-Path $repo 'agents\scripts\bind_skills_to_toolboxes.py')
}

# ---------------------------------------------------------------------------
# 5. Map infra outputs into the azd agent environment
# ---------------------------------------------------------------------------
Write-Host "== 5. Configuring azd agent environment ==" -ForegroundColor Cyan
$fsiMcpKey = & (Join-Path $repo 'scripts\set_azd_env_from_infra.ps1') `
    -ProjectEndpoint $projectEndpoint -StorageBlobEndpoint $storageBlob `
    -ModelDeploymentName $ModelDeploymentName -SecEdgarMcpUrl $secMcpUrl `
    -FsiMcpKey $fsiMcpKey -EnvName $EnvName -AzdDir $azdDir | Select-Object -Last 1
Push-Location $azdDir
azd env set AZURE_SUBSCRIPTION_ID (az account show --query id -o tsv) | Out-Null
azd env set AZURE_LOCATION $Location | Out-Null
Pop-Location

# ---------------------------------------------------------------------------
# 6. Deploy the 3 hosted agents
# ---------------------------------------------------------------------------
if (-not $SkipAgents) {
    Write-Host "== 6. Deploying hosted agents ==" -ForegroundColor Cyan
    # Sync runtime source into the azd agent-src copy (critical: stale copies ship old behavior).
    Copy-Item (Join-Path $hostedDir 'fsi_hosted_agent_v3.py') (Join-Path $azdDir 'agent-src\fsi_hosted_agent_v3.py') -Force
    Copy-Item (Join-Path $hostedDir 'fsi_artifact_egress.py') (Join-Path $azdDir 'agent-src\fsi_artifact_egress.py') -Force
    Copy-Item (Join-Path $hostedDir 'requirements.txt')       (Join-Path $azdDir 'agent-src\requirements.txt') -Force

    $env:GH_TOKEN = (gh auth token)
    $env:GITHUB_TOKEN = $env:GH_TOKEN
    $env:AZD_AGENT_SKIP_ACR = 'true'

    Push-Location $azdDir
    try {
        foreach ($svc in @('fsi-equity', 'fsi-ib-pitch', 'fsi-pe-lbo')) {
            $ok = $false
            for ($i = 1; $i -le 3 -and -not $ok; $i++) {
                Write-Host "  azd deploy $svc (attempt $i)"
                azd deploy $svc -e $EnvName --no-prompt
                if ($LASTEXITCODE -eq 0) { $ok = $true } else { Start-Sleep 15 }
            }
            if (-not $ok) { throw "azd deploy $svc failed after 3 attempts." }
        }
    } finally { Pop-Location }

    # 7. Grant agent instance-identity RBAC (identities exist only after deploy).
    Write-Host "== 7. Granting agent RBAC ==" -ForegroundColor Cyan
    & (Join-Path $repo 'scripts\grant_agent_rbac.ps1') `
        -ResourceGroup $rg -AiAccountName $aiAccount -StorageAccountName $storageAccount
}

# ---------------------------------------------------------------------------
# 8. Build + deploy API and portal images
# ---------------------------------------------------------------------------
if (-not $SkipApps) {
    Write-Host "== 8. Building + deploying API and portal ==" -ForegroundColor Cyan
    az acr build --registry $acrName --image fsi-api:latest (Join-Path $repo 'api') | Out-Null
    az containerapp update -n "ca-api-$EnvName" -g $rg `
        --image "$acrName.azurecr.io/fsi-api:latest" `
        --revision-suffix ("v" + (Get-Date -Format 'MMddHHmmss')) | Out-Null

    az acr build --registry $acrName --image fsi-portal:latest `
        --build-arg NEXT_PUBLIC_API_BASE_URL=$apiUrl (Join-Path $repo 'portal') | Out-Null
    az containerapp update -n "ca-portal-$EnvName" -g $rg `
        --image "$acrName.azurecr.io/fsi-portal:latest" `
        --revision-suffix ("v" + (Get-Date -Format 'MMddHHmmss')) | Out-Null
}

# ---------------------------------------------------------------------------
# 9. Validate
# ---------------------------------------------------------------------------
if (-not $SkipValidate) {
    Write-Host "== 9. Validating scenarios ==" -ForegroundColor Cyan
    $env:API_BASE_URL = $apiUrl
    python (Join-Path $repo 'scripts\validate.py')
}

Write-Host ""
Write-Host "Done. Portal: $portalUrl" -ForegroundColor Green
Write-Host "      API:    $apiUrl" -ForegroundColor Green
