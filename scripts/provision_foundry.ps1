<#
.SYNOPSIS
    Declaratively provision the Foundry skills, SEC EDGAR connection and the 3
    scenario toolboxes with the GA `azd ai` extensions (skills / connections /
    toolboxes).

.DESCRIPTION
    Replaces the earlier data-plane REST scripts (provision_skills.py,
    create_toolboxes.py, bind_skills_to_toolboxes.py) with the GA-supported
    declarative flow:

      1. Register the 12 Anthropic financial-analysis skills as Foundry skills
         (`azd ai skill create <name> --file SKILL.md --force`). Skill content is
         fetched at run time from a pinned Anthropic commit, then optionally
         overlaid with repo-local instructions from `skills/overrides/`.
      2. (optional) Register the self-hosted SEC EDGAR MCP server as a GOVERNED
         remote-tool project CONNECTION named `sec-edgar`, carrying the shared
         secret in a custom header. Its tools then namespace as `sec-edgar___*`.
      3. Create + publish the 3 scenario toolboxes from declarative files. Each
         toolbox attaches: the `sec-edgar` connection (when configured), the
         built-in `web_search` tool (named `web`), the GA Tool Search meta-tool
         `toolbox_search_preview` (named `tool_search`), and its scenario skills.
         code_interpreter is DELIBERATELY OMITTED — it runs as the Foundry-native
         hosted tool in the agent because artifact egress depends on the native
         sandbox container id, and omitting it also stops the model shadowing that
         native path via Tool Search.

    With Tool Search enabled each toolbox's MCP `tools/list` returns ONLY
    `tool_search` + `call_tool`; the agent discovers `web` and the full
    `sec-edgar___*` surface through them. Skills continue to surface as MCP
    resources (`skill://...`) for the SDK `load_skill` progressive-disclosure path.

.NOTES
    Requires the azd `azure.ai.skills`, `azure.ai.connections` and
    `azure.ai.toolboxes` extensions and an azd environment whose
    FOUNDRY_PROJECT_ENDPOINT points at the target project. Run AFTER
    set_azd_env_from_infra.ps1 (which creates the env and seeds that variable).
    Idempotent: skills/connection use --force; toolboxes are deleted (if present)
    then recreated + published.

    azd credential subprocess calls flake under load (`AzureDeveloperCLICredential:
    exit status 1`), so every azd call is wrapped in a retry loop.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$EnvName,
    [string]$AzdDir = (Join-Path $PSScriptRoot '..\agents\hosted\_azd'),
    [string]$SkillsRef = '4aa51ed3d379731f8f9beff498d749580372699c',
    [string]$SecEdgarMcpUrl = '',
    [string]$FsiMcpKey = '',
    [string]$FsiMcpKeyHeader = 'x-fsi-mcp-key',
    [string[]]$SkillsOnly = @()
)

$ErrorActionPreference = 'Stop'
$AzdDir = (Resolve-Path $AzdDir).Path

    $RawBase = "https://raw.githubusercontent.com/anthropics/financial-services/$SkillsRef/plugins/vertical-plugins/financial-analysis/skills"

# The 12 runtime skills (skill-creator intentionally excluded).
$RuntimeSkills = @(
    '3-statement-model', 'audit-xls', 'clean-data-xls', 'competitive-analysis',
    'comps-analysis', 'dcf-model', 'deck-refresh', 'ib-check-deck', 'lbo-model',
    'ppt-template-creator', 'pptx-author', 'xlsx-author'
)

# scenario toolbox -> @{ description; skills[] }. Cross-cutting skills
# (xlsx-author, clean-data-xls, audit-xls) are referenced from multiple toolboxes;
# the single central skill is the source of truth.
$Toolboxes = [ordered]@{
    'tb-equity-research' = @{
        description = 'S1 Equity Research & Valuation: DCF / comps / 3-statement modelling with Excel authoring, data cleaning and model audit skills; live web/SEC grounding via Tool Search.'
        skills      = @('3-statement-model', 'dcf-model', 'comps-analysis', 'xlsx-author', 'clean-data-xls', 'audit-xls')
    }
    'tb-ib-pitch'        = @{
        description = 'S2 Investment Banking Pitch: competitive & comps analysis, PPTX deck authoring, template creation, deck refresh and deck QC skills; live web/SEC grounding via Tool Search.'
        skills      = @('competitive-analysis', 'comps-analysis', 'pptx-author', 'ppt-template-creator', 'deck-refresh', 'ib-check-deck', 'xlsx-author')
    }
    'tb-pe-lbo'          = @{
        description = 'S3 Private Equity LBO Screening: LBO modelling with Excel authoring, data cleaning and model audit skills; live web/SEC grounding via Tool Search.'
        skills      = @('lbo-model', 'xlsx-author', 'clean-data-xls', 'audit-xls')
    }
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Run an azd command with retries. azd's AzureDeveloperCLICredential shells out to
# the CLI to mint tokens and intermittently fails (`exit status 1`) under back-to-back
# calls; retry those transient failures. Returns captured stdout+stderr; throws on
# persistent failure.
function Invoke-Azd {
    param(
        [Parameter(Mandatory = $true)][string[]]$Args,
        [int]$Retries = 8,
        [int]$DelaySeconds = 4,
        [string]$What = ''
    )
    if (-not $What) { $What = ($Args -join ' ') }
    $last = ''
    for ($i = 1; $i -le $Retries; $i++) {
        $out = & azd @Args 2>&1 | Out-String
        if ($LASTEXITCODE -eq 0) { return $out }
        $last = $out
        $transient = $out -match 'AzureDeveloperCLICredential|exit status 1|deadline|timeout|TooManyRequests|429|temporarily'
        if (-not $transient) { break }
        Write-Host ("  [retry $i/$Retries] azd $What (transient auth/throttle)")
        Start-Sleep $DelaySeconds
    }
    throw "azd $What failed after $i attempt(s):`n$last"
}

# azd emits a stderr status line (e.g. "2026/07/13 ... toolbox list: resolved project
# endpoint ... (source=azdEnv)") BEFORE the JSON payload; because Invoke-Azd captures
# 2>&1 that noise is interleaved with the JSON and plain ConvertFrom-Json chokes on it.
# Extract the JSON value (first { or [ to the matching last } or ]) before parsing.
function ConvertFrom-AzdJson {
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    $start = $Text.IndexOfAny([char[]]@('{', '['))
    if ($start -lt 0) { return $null }
    $close = if ($Text[$start] -eq '{') { '}' } else { ']' }
    $end = $Text.LastIndexOf($close)
    if ($end -lt $start) { return $null }
    try { return $Text.Substring($start, $end - $start + 1) | ConvertFrom-Json -ErrorAction Stop }
    catch { return $null }
}

# Reliably delete a toolbox (all versions) so `create` is idempotent. The delete call
# hits the same intermittent AzureDeveloperCLICredential failures as everything else, and
# a silently-failed delete makes the subsequent `create` abort with "already exists". So
# retry until `toolbox list` confirms the name is gone (or was never there).
function Remove-ToolboxReliably {
    param([string]$Name, [string]$Env, [int]$Retries = 8, [int]$DelaySeconds = 4)
    for ($i = 1; $i -le $Retries; $i++) {
        $listObj = ConvertFrom-AzdJson (& azd ai toolbox list -e $Env -o json 2>&1 | Out-String)
        if ($listObj -and -not (@($listObj.toolboxes | Where-Object { $_.name -eq $Name }).Count)) {
            return  # confirmed absent
        }
        & azd ai toolbox delete $Name -e $Env --no-prompt --force 2>&1 | Out-Null
        Start-Sleep $DelaySeconds
    }
    $listObj = ConvertFrom-AzdJson (& azd ai toolbox list -e $Env -o json 2>&1 | Out-String)
    if ($listObj -and (@($listObj.toolboxes | Where-Object { $_.name -eq $Name }).Count)) {
        throw "could not delete existing toolbox $Name after $Retries attempts"
    }
}

# Fetch a skill's SKILL.md from pinned GitHub raw, retrying transient 429/5xx.
function Get-UpstreamSkillMd {
    param([string]$Name, [int]$Attempts = 6)
    $url = "$RawBase/$Name/SKILL.md"
    for ($i = 1; $i -le $Attempts; $i++) {
        try {
            return (Invoke-WebRequest -Uri $url -Headers @{ 'User-Agent' = 'fsi-skill-provisioner' } -UseBasicParsing).Content
        } catch {
            $code = $_.Exception.Response.StatusCode.value__ 2>$null
            if ($i -lt $Attempts -and ($null -eq $code -or $code -in 429, 500, 502, 503, 504)) {
                $delay = [Math]::Min([Math]::Pow(2, $i), 30)
                Write-Host ("  [fetch retry $i/$Attempts] {0}: HTTP {1}; sleeping {2}s" -f $Name, $code, $delay)
                Start-Sleep -Seconds $delay
                continue
            }
            throw
        }
    }
}

function Get-SkillMd {
    param([string]$Name)

    $base = Get-UpstreamSkillMd -Name $Name
    $overrideRoot = Join-Path $PSScriptRoot '..\skills\overrides'
    $fullOverride = Join-Path $overrideRoot "$Name.SKILL.md"
    $appendOverride = Join-Path $overrideRoot "$Name.append.md"

    if (Test-Path -LiteralPath $fullOverride) {
        return [System.IO.File]::ReadAllText($fullOverride)
    }
    if (Test-Path -LiteralPath $appendOverride) {
        $overlay = [System.IO.File]::ReadAllText($appendOverride).Trim()
        if ($overlay) {
            return ($base.TrimEnd() + "`r`n`r`n" + $overlay + "`r`n")
        }
    }
    return $base
}

Push-Location $AzdDir
try {
    azd env select $EnvName | Out-Null

    if (-not $SecEdgarMcpUrl -or -not $FsiMcpKey) {
        $envLines = @(& azd env get-values -e $EnvName 2>$null)
        foreach ($line in $envLines) {
            if (-not $SecEdgarMcpUrl -and $line -match '^SEC_EDGAR_MCP_URL="?(.+?)"?$') {
                $SecEdgarMcpUrl = $Matches[1]
            }
            if (-not $FsiMcpKey -and $line -match '^FSI_MCP_KEY="?(.+?)"?$') {
                $FsiMcpKey = $Matches[1]
            }
        }
    }

    # -----------------------------------------------------------------------
    # 1. Register skills
    # -----------------------------------------------------------------------
    $skills = if ($SkillsOnly.Count) { $RuntimeSkills | Where-Object { $SkillsOnly -contains $_ } } else { $RuntimeSkills }
    Write-Host "== Registering $($skills.Count) skills (ref $($SkillsRef.Substring(0,7))) ==" -ForegroundColor Cyan
    $tmpRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("fsi-skills-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $tmpRoot -Force | Out-Null
    try {
        foreach ($s in $skills) {
            $md = Get-SkillMd -Name $s
            $file = Join-Path $tmpRoot "$s.md"
            [System.IO.File]::WriteAllText($file, $md)
            Invoke-Azd -Args @('ai', 'skill', 'create', $s, '--file', $file, '--force', '-e', $EnvName) -What "skill create $s" | Out-Null
            Write-Host "  [OK] skill $s"
        }
    } finally {
        Remove-Item -Recurse -Force $tmpRoot -ErrorAction SilentlyContinue
    }

    # -----------------------------------------------------------------------
    # 2. SEC EDGAR remote-tool connection (optional)
    # -----------------------------------------------------------------------
    $hasSec = [bool]$SecEdgarMcpUrl
    if ($hasSec) {
        Write-Host "== Registering SEC EDGAR remote-tool connection 'sec-edgar' ==" -ForegroundColor Cyan
        $connArgs = @('ai', 'connection', 'create', 'sec-edgar',
            '--kind', 'remote-tool', '--target', $SecEdgarMcpUrl, '--force', '-e', $EnvName)
        if ($FsiMcpKey) {
            $connArgs += @('--auth-type', 'custom-keys', '--custom-key', "$FsiMcpKeyHeader=$FsiMcpKey")
        } else {
            $connArgs += @('--auth-type', 'none')
        }
        Invoke-Azd -Args $connArgs -What 'connection create sec-edgar' | Out-Null
        Write-Host "  [OK] connection sec-edgar -> $SecEdgarMcpUrl"
    } else {
        Write-Host "== SEC EDGAR connection OMITTED (no -SecEdgarMcpUrl) ==" -ForegroundColor DarkGray
    }

    # -----------------------------------------------------------------------
    # 3. Create + publish the 3 scenario toolboxes
    # -----------------------------------------------------------------------
    Write-Host "== Creating + publishing $($Toolboxes.Count) scenario toolboxes ==" -ForegroundColor Cyan
    $tbTmp = Join-Path ([System.IO.Path]::GetTempPath()) ("fsi-tb-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $tbTmp -Force | Out-Null
    try {
        foreach ($name in $Toolboxes.Keys) {
            $def = $Toolboxes[$name]

            # Build the declarative toolbox document (OpenAI.Tool shape for `tools`).
            # NB: NO code_interpreter (native only); artifact egress depends on the
            # native sandbox container id. web + tool_search are the only tools, and
            # Tool Search hides them behind the two meta-tools at runtime.
            $tools = @(
                [ordered]@{ type = 'web_search'; name = 'web' },
                [ordered]@{ type = 'toolbox_search_preview'; name = 'tool_search' }
            )
            $doc = [ordered]@{
                description = $def.description
                tools       = $tools
                skills      = @($def.skills | ForEach-Object { [ordered]@{ name = $_ } })
            }
            if ($hasSec) { $doc['connections'] = @([ordered]@{ name = 'sec-edgar' }) }

            $file = Join-Path $tbTmp "$name.json"
            ($doc | ConvertTo-Json -Depth 6) | Set-Content -Path $file -Encoding utf8

            # Recreate for idempotency: delete any existing toolbox (all versions) first.
            Remove-ToolboxReliably -Name $name -Env $EnvName

            $createOut = Invoke-Azd -Args @('ai', 'toolbox', 'create', $name, '--from-file', $file, '-e', $EnvName, '-o', 'json') -What "toolbox create $name"

            # Determine the version to promote. azd emits snake_case JSON (default_version,
            # versions[].version), sometimes prefixed by a stderr status line, so parse
            # defensively across the create output and a `versions list` fallback.
            $version = $null
            $obj = ConvertFrom-AzdJson $createOut
            if ($obj) {
                foreach ($cand in @($obj.version, $obj.default_version)) {
                    if ($cand) { $version = [string]$cand; break }
                }
                if (-not $version -and $obj.versions) {
                    $version = ($obj.versions | ForEach-Object { $_.version } | Where-Object { $_ } | Sort-Object { [int]$_ } -Descending | Select-Object -First 1)
                }
                if (-not $version -and $obj.id -match ':(\d+)$') { $version = $Matches[1] }
            }
            if (-not $version) {
                $vout = Invoke-Azd -Args @('ai', 'toolbox', 'versions', 'list', $name, '-e', $EnvName, '-o', 'json') -What "toolbox versions list $name"
                $vobj = ConvertFrom-AzdJson $vout
                if ($vobj) {
                    if ($vobj.default_version) { $version = [string]$vobj.default_version }
                    if (-not $version -and $vobj.versions) {
                        $version = ($vobj.versions | ForEach-Object { $_.version } | Where-Object { $_ } | Sort-Object { [int]$_ } -Descending | Select-Object -First 1)
                    }
                }
            }
            if (-not $version) { throw "could not determine created version for toolbox $name" }

            Invoke-Azd -Args @('ai', 'toolbox', 'publish', $name, [string]$version, '-e', $EnvName) -What "toolbox publish $name $version" | Out-Null
            Write-Host "  [OK] toolbox $name -> v$version (default); $($def.skills.Count) skills, SEC $(if($hasSec){'BOUND'}else{'omitted'})"
        }
    } finally {
        Remove-Item -Recurse -Force $tbTmp -ErrorAction SilentlyContinue
    }

    Write-Host "Foundry declarative provisioning complete." -ForegroundColor Green
}
finally {
    Pop-Location
}
