<#
.SYNOPSIS
  One-command local screenshot capture for the FSI demo README.

.DESCRIPTION
  Produces the images embedded in README.md and fails fast if the portal only returns
  fallback `*_agent_summary.*` artifacts instead of the real generated files:
    1. Portal UI (Playwright/Chromium) — scenario gallery + a completed run per scenario,
       and downloads each produced artifact (.xlsx / .pptx).
    2. Rendered artifacts (Office COM) — turns the downloaded workbook/deck into PNGs.

  Everything runs against a LIVE, already-deployed stack (portal + API). No Azure SDK,
  no secrets. Rendered PNGs land in docs/images/ (committed); raw downloaded Office
  files land in docs/images/_artifacts/ (gitignored).

  Prereqs: Node.js + npm (Playwright is installed on first run) and, for artifact
  rendering, Microsoft Excel + PowerPoint installed locally.

.PARAMETER PortalUrl
  Base URL of the deployed portal Container App (required).

.PARAMETER Scenarios
  Comma-separated scenario keys to run (default: equity-research,ib-pitch — one .xlsx +
  one .pptx, enough to show both artifact types).

.PARAMETER OutDir
  Output directory for PNGs + manifest (default: docs/images).

.PARAMETER SkipRun
  Only capture the landing gallery; do not run any scenario.

.PARAMETER SkipOffice
  Skip the Office-render step (portal screenshots + artifact downloads only).

.PARAMETER RunTimeoutSec
  Per-scenario completion timeout in seconds (default: 600).

.PARAMETER Headed
  Show the browser instead of running headless.

.EXAMPLE
  ./scripts/capture_screenshots.ps1 -PortalUrl https://ca-portal-myenv.<region>.azurecontainerapps.io
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)] [string] $PortalUrl,
  [string] $Scenarios = 'equity-research,ib-pitch',
  [string] $OutDir,
  [switch] $SkipRun,
  [switch] $SkipOffice,
  [int] $RunTimeoutSec = 600,
  [switch] $Headed
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$shotDir = Join-Path $PSScriptRoot 'screenshots'
if (-not $OutDir) { $OutDir = Join-Path $repoRoot 'docs\images' }
if (-not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }
$OutDir = (Resolve-Path -LiteralPath $OutDir).Path
$artifactDir = Join-Path $OutDir '_artifacts'
$manifestPath = Join-Path $OutDir 'manifest.json'

function Get-CanonicalArtifactImageName {
  param(
    [string] $Scenario,
    [string] $Extension
  )
  switch ("$Scenario|$Extension".ToLowerInvariant()) {
    'equity-research|.xlsx' { return 'artifact-equity-dcf.png' }
    'ib-pitch|.pptx'        { return 'artifact-ib-pitch-slide.png' }
    default                 { return $null }
  }
}

function Get-BestRenderedArtifactImage {
  param(
    [string[]] $Paths
  )
  $items = @(
    $Paths |
      Where-Object { $_ -and (Test-Path -LiteralPath $_) } |
      ForEach-Object { Get-Item -LiteralPath $_ }
  )
  if (-not $items) { return $null }
  return ($items | Sort-Object Length -Descending | Select-Object -First 1).FullName
}

function Get-PreferredRenderedArtifactImage {
  param(
    [string] $Scenario,
    [string] $Extension,
    [string[]] $Paths
  )
  $existing = @($Paths | Where-Object { $_ -and (Test-Path -LiteralPath $_) })
  if (-not $existing) { return $null }

  if ($Scenario -eq 'ib-pitch' -and $Extension -eq '.pptx') {
    foreach ($preferred in @('-slide2.png', '-slide3.png')) {
      $match = $existing | Where-Object { $_.ToLowerInvariant().EndsWith($preferred) } | Select-Object -First 1
      if ($match) { return $match }
    }
  }

  return Get-BestRenderedArtifactImage -Paths $existing
}

Write-Host "== FSI screenshot capture ==" -ForegroundColor Cyan
Write-Host "Portal    : $PortalUrl"
Write-Host "Scenarios : $Scenarios"
Write-Host "Out dir   : $OutDir"

# --- 1. Install Playwright (idempotent) ------------------------------------------
Push-Location $shotDir
try {
  if (-not (Test-Path (Join-Path $shotDir 'node_modules\playwright'))) {
    Write-Host "`n[1/3] Installing Playwright..." -ForegroundColor Cyan
    npm install --silent
    if ($LASTEXITCODE -ne 0) { throw "npm install failed in $shotDir" }
  } else {
    Write-Host "`n[1/3] Playwright already installed." -ForegroundColor Cyan
  }
  # Ensure the Chromium browser binary is present (no-op if already downloaded).
  npx --yes playwright install chromium | Out-Host
  if ($LASTEXITCODE -ne 0) { throw "playwright install chromium failed" }

  # --- 2. Capture the portal ------------------------------------------------------
  Write-Host "`n[2/3] Capturing portal UI..." -ForegroundColor Cyan
  $env:PORTAL_URL = $PortalUrl
  $env:OUT_DIR = $OutDir
  $env:ARTIFACT_DIR = $artifactDir
  $env:SCENARIOS = $Scenarios
  $env:RUN = if ($SkipRun) { 'false' } else { 'true' }
  $env:RUN_TIMEOUT_MS = ($RunTimeoutSec * 1000).ToString()
  $env:HEADED = if ($Headed) { 'true' } else { 'false' }
  node capture_portal.mjs
  if ($LASTEXITCODE -ne 0) { throw "portal capture failed validation" }
}
finally {
  Pop-Location
}

# --- 3. Render downloaded artifacts to PNG ---------------------------------------
if ($SkipOffice) {
  Write-Host "`n[3/3] Skipping Office render (per -SkipOffice)." -ForegroundColor Yellow
} elseif (-not (Test-Path -LiteralPath $manifestPath)) {
  Write-Host "`n[3/3] No manifest found; nothing to render." -ForegroundColor Yellow
} else {
  Write-Host "`n[3/3] Rendering artifacts to PNG (Office COM)..." -ForegroundColor Cyan
  $officeScript = Join-Path $shotDir 'capture_office.ps1'
  $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
  $artifacts = @($manifest.artifacts | Where-Object { $_.file })
  if (-not $artifacts) {
    Write-Host "  (manifest contains no downloaded artifacts)" -ForegroundColor Yellow
  }
  foreach ($entry in $artifacts) {
    $f = Get-Item -LiteralPath (Join-Path $OutDir ($entry.file -replace '/', '\'))
    $kind = if ($f.Extension -match 'xls') { 'xlsx' } else { 'pptx' }
    # Derive scenario from the manifest if available, else from the filename.
    $base = "artifact-$kind-$([System.IO.Path]::GetFileNameWithoutExtension($f.Name))"
    $base = ($base -replace '[^A-Za-z0-9._-]', '-')
    Write-Host "  rendering $($f.Name)..."
    try {
      $renderArgs = @(
        '-InputFile', $f.FullName,
        '-OutDir', $OutDir,
        '-BaseName', $base
      )
      if ($kind -eq 'pptx') { $renderArgs += @('-MaxSlides', '3') }
      $produced = @(
        (& $officeScript @renderArgs) |
        ForEach-Object { [string]$_ } |
        ForEach-Object { $_ -split "(`r`n|`n)" } |
        ForEach-Object { $_.Trim() } |
          Where-Object {
            $_ -and
            $_ -match '^[A-Za-z]:\\.*\.png$' -and
            (Test-Path -LiteralPath $_)
          }
      )
      $canonical = Get-CanonicalArtifactImageName -Scenario $entry.scenario -Extension $f.Extension
      if ($canonical -and $produced.Count -gt 0) {
        $best = Get-PreferredRenderedArtifactImage -Scenario $entry.scenario -Extension $f.Extension.ToLowerInvariant() -Paths $produced
        if ($best) {
          Copy-Item -LiteralPath $best -Destination (Join-Path $OutDir $canonical) -Force
          Write-Host "  aliased $best -> $canonical"
        }
      }
    } catch {
      Write-Warning "  failed to render $($f.Name): $($_.Exception.Message)"
    }
  }
}

Write-Host "`nDone. Images in: $OutDir" -ForegroundColor Green
Get-ChildItem -LiteralPath $OutDir -Filter *.png | Select-Object Name, Length | Format-Table -AutoSize
