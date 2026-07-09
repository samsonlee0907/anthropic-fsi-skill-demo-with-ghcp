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
    [string]$ModelDeploymentName = 'gpt-5.4',
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

# Best-effort UTF-8 for our own console output. NOTE: this does NOT fix `az acr build`
# log streaming -- az launches `python.exe -I` (isolated) which ignores PYTHONUTF8, so
# that path is handled separately by Invoke-AcrBuild (status polling, no log streaming).
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

function Require-Tool([string]$name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        throw "Required tool '$name' not found on PATH."
    }
}

# Throw on non-zero exit of the most recent native command (az/azd don't raise
# terminating errors, so $ErrorActionPreference='Stop' does NOT catch them).
function Assert-LastExit([string]$what) {
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)." }
}

# Build an image in ACR without depending on log streaming.
# `az acr build` streams server-side build logs and, on Windows, crashes with a
# cosmetic `UnicodeEncodeError` (colorama writing UTF-8 to a cp1252 stdout). Because
# `az.cmd` launches `python.exe -I` (isolated mode), PYTHONUTF8/PYTHONIOENCODING are
# ignored and the console code page can't fix a piped stream -- so we cannot prevent
# the crash. Instead: capture the queued run id (printed before any crash), ignore the
# cosmetic streaming failure, and poll the run status authoritatively (that call does
# not stream logs). This is portable across OS and console code pages.
function Invoke-AcrBuild {
    param(
        [string]$Registry,
        [string]$Image,
        [string]$Context,
        [string[]]$BuildArgs = @()
    )
    $cmd = @('acr', 'build', '--registry', $Registry, '--image', $Image)
    foreach ($ba in $BuildArgs) { $cmd += @('--build-arg', $ba) }
    $cmd += $Context
    $out = az @cmd 2>&1 | Out-String
    $runId = [regex]::Match($out, 'Queued a build with ID:\s*(\S+)').Groups[1].Value
    if (-not $runId) {
        Write-Host $out
        throw "az acr build ($Image) failed before a run was queued."
    }
    Write-Host "  ACR build $runId queued for $Image; polling status..."
    while ($true) {
        $status = az acr task show-run --registry $Registry --run-id $runId --query status -o tsv 2>$null
        switch ($status) {
            'Succeeded' { Write-Host "  ACR build $runId Succeeded."; return }
            { $_ -in 'Failed', 'Error', 'Canceled', 'Timeout' } { throw "ACR build $runId ($Image) ended: $status" }
        }
        Start-Sleep 5
    }
}

Write-Host "== Preflight ==" -ForegroundColor Cyan
'az', 'azd', 'python', 'gh' | ForEach-Object { Require-Tool $_ }

# Fail fast if the provisioning scripts' Python deps are missing. Without this the run
# provisions (and bills) infra in step 1, then dies with a bare ModuleNotFoundError in
# step 2. Point the user straight at the pinned requirements file instead.
if (-not $SkipSkills) {
    python -c "import azure.ai.projects, azure.identity" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "Missing Python dependencies for the provisioning scripts. Run: pip install -r agents/scripts/requirements.txt"
    }
}

# Verify the azd Foundry-agents extension is present (provides hosted-agent `azd deploy`
# and `azd ai agent`). Skip the check when not deploying agents.
if (-not $SkipAgents) {
    $agentsExt = azd extension list --installed 2>$null | Select-String -Pattern 'azure\.ai\.agents'
    if (-not $agentsExt) {
        throw "azd 'azure.ai.agents' extension not installed. Run: azd extension install azure.ai.agents"
    }
}

if ($SubscriptionId) { az account set --subscription $SubscriptionId | Out-Null }
if (-not $PrincipalId) { $PrincipalId = az ad signed-in-user show --query id -o tsv }
az config set auth.useAzCliAuth true 2>$null | Out-Null

# ---------------------------------------------------------------------------
# 1. Infra
# ---------------------------------------------------------------------------
if (-not $SkipInfra) {
    Write-Host "== 1. Provisioning infra ($EnvName / $Location) ==" -ForegroundColor Cyan
    $depJson = az deployment sub create `
        --name "fsi-$EnvName" `
        --location $Location `
        --template-file (Join-Path $repo 'infra\main.bicep') `
        --parameters environmentName=$EnvName location=$Location `
                     developerPrincipalId=$PrincipalId `
                     agentModelDeploymentName=$ModelDeploymentName `
        -o json
    Assert-LastExit 'az deployment sub create'
    $dep = $depJson | ConvertFrom-Json
} else {
    Write-Host "== 1. Reading existing infra outputs ==" -ForegroundColor Cyan
    $depJson = az deployment sub show --name "fsi-$EnvName" -o json
    Assert-LastExit 'az deployment sub show'
    $dep = $depJson | ConvertFrom-Json
}

$o = $dep.properties.outputs
$projectEndpoint  = $o.AZURE_AI_PROJECT_ENDPOINT.value
$projectId        = $o.AZURE_AI_PROJECT_ID.value
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

# Guard: an Azure Policy 'modify' effect can flip storage publicNetworkAccess to
# Disabled at ARM-create time even though the bicep sets Enabled. With no private
# endpoints for the (VNet-less) hosted-agent compute + Container Apps, that breaks
# artifact egress with an AuthorizationFailure that looks exactly like a missing RBAC
# role. Re-assert Enabled here so the egress path stays reachable over AAD-gated public.
if (-not $SkipInfra) {
    $pna = az storage account show -n $storageAccount -g $rg --query publicNetworkAccess -o tsv 2>$null
    if ($pna -and $pna -ne 'Enabled') {
        Write-Host "  [guard] storage publicNetworkAccess=$pna; re-enabling ..." -ForegroundColor Yellow
        az storage account update -n $storageAccount -g $rg --public-network-access Enabled 1>$null 2>$null
        Assert-LastExit 'az storage account update (publicNetworkAccess)'
    }
}

# ---------------------------------------------------------------------------
# 2. Register skills + create toolboxes
# ---------------------------------------------------------------------------
if (-not $SkipSkills) {
    Write-Host "== 2. Registering skills + toolboxes ==" -ForegroundColor Cyan
    python (Join-Path $repo 'agents\scripts\provision_skills.py')
    Assert-LastExit "Skill registration (provision_skills.py)"
    python (Join-Path $repo 'agents\scripts\create_toolboxes.py')
    Assert-LastExit "Toolbox creation (create_toolboxes.py)"
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
    Assert-LastExit "Skill-to-toolbox binding (bind_skills_to_toolboxes.py)"
}

# ---------------------------------------------------------------------------
# 5. Map infra outputs into the azd agent environment
# ---------------------------------------------------------------------------
Write-Host "== 5. Configuring azd agent environment ==" -ForegroundColor Cyan
$fsiMcpKey = & (Join-Path $repo 'scripts\set_azd_env_from_infra.ps1') `
    -ProjectEndpoint $projectEndpoint -StorageBlobEndpoint $storageBlob `
    -ProjectId $projectId `
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
        -ResourceGroup $rg -AiAccountName $aiAccount -StorageAccountName $storageAccount `
        -ProjectEndpoint $projectEndpoint -AgentNames @('fsi-equity', 'fsi-ib-pitch', 'fsi-pe-lbo')
}

# ---------------------------------------------------------------------------
# 7b. Ensure storage stays network-reachable (runs on every invocation).
# A subscription Azure Policy can flip publicNetworkAccess back to Disabled AFTER the
# bicep sets it Enabled. When that happens the hosted-agent compute and the VNet-less
# Container Apps BFF can no longer reach Blob Storage, and artifact upload fails with
# AuthorizationFailure -- the SAME wording as a missing RBAC role, so it is easy to
# misdiagnose as an identity problem. Re-assert Enabled here so egress keeps working.
# (Data stays protected by Entra ID RBAC only; shared-key + anonymous access remain off.)
# ---------------------------------------------------------------------------
Write-Host "== 7b. Ensuring storage public network access ==" -ForegroundColor Cyan
az storage account update -n $storageAccount -g $rg `
    --public-network-access Enabled --default-action Allow --bypass AzureServices | Out-Null
Assert-LastExit 'az storage account update (public network access)'

# ---------------------------------------------------------------------------
# 8. Build + deploy API and portal images
# ---------------------------------------------------------------------------
if (-not $SkipApps) {
    Write-Host "== 8. Building + deploying API and portal ==" -ForegroundColor Cyan
    Invoke-AcrBuild -Registry $acrName -Image 'fsi-api:latest' -Context (Join-Path $repo 'api')
    az containerapp update -n "ca-api-$EnvName" -g $rg `
        --image "$acrName.azurecr.io/fsi-api:latest" `
        --revision-suffix ("v" + (Get-Date -Format 'MMddHHmmss')) | Out-Null
    Assert-LastExit 'az containerapp update (api)'

    Invoke-AcrBuild -Registry $acrName -Image 'fsi-portal:latest' -Context (Join-Path $repo 'portal') `
        -BuildArgs @("NEXT_PUBLIC_API_BASE_URL=$apiUrl")
    az containerapp update -n "ca-portal-$EnvName" -g $rg `
        --image "$acrName.azurecr.io/fsi-portal:latest" `
        --revision-suffix ("v" + (Get-Date -Format 'MMddHHmmss')) | Out-Null
    Assert-LastExit 'az containerapp update (portal)'
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
