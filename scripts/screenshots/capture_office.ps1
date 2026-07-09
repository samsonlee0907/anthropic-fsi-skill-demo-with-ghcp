<#
.SYNOPSIS
  Render a generated Office artifact (.xlsx / .pptx) to PNG image(s) for the README.

.DESCRIPTION
  Uses local Microsoft Office COM automation (Excel / PowerPoint) — no third-party
  tools, no LibreOffice — to turn an agent-produced workbook or deck into flat images:

    * .pptx : exports the first -MaxSlides slides via Slide.Export (native PNG).
    * .xlsx : for the first -MaxSheets non-empty worksheets, copies the used range as a
              picture, pastes it into a temporary in-sheet chart, and exports that chart
              (Chart.Export) — a deterministic, clipboard-free range->PNG method.

  Requires Excel / PowerPoint installed (COM ProgIDs Excel.Application /
  PowerPoint.Application). Intended to run on a developer workstation, not in CI.

.PARAMETER InputFile
  Path to the .xlsx or .pptx artifact to render.

.PARAMETER OutDir
  Directory to write PNGs into (created if missing).

.PARAMETER BaseName
  Base file name for the output PNGs (default: input file's base name, sanitized).

.PARAMETER MaxSlides
  Max PowerPoint slides to export (default 2).

.PARAMETER MaxSheets
  Max Excel worksheets to export (default 2).

.PARAMETER MaxRows
  Cap the Excel used-range height so large models stay legible (default 45 rows).

.EXAMPLE
  ./capture_office.ps1 -InputFile ..\..\docs\images\_artifacts\MSFT_valuation.xlsx `
    -OutDir ..\..\docs\images -BaseName artifact-equity-xlsx
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)] [string] $InputFile,
  [Parameter(Mandatory = $true)] [string] $OutDir,
  [string] $BaseName,
  [int] $MaxSlides = 2,
  [int] $MaxSheets = 2,
  [int] $MaxRows = 45
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $InputFile)) { throw "Input file not found: $InputFile" }
$InputFile = (Resolve-Path -LiteralPath $InputFile).Path
if (-not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }
$OutDir = (Resolve-Path -LiteralPath $OutDir).Path

if (-not $BaseName) {
  $BaseName = [System.IO.Path]::GetFileNameWithoutExtension($InputFile)
}
# sanitize base name for a safe file stem
$BaseName = ($BaseName -replace '[^A-Za-z0-9._-]', '-')

$ext = [System.IO.Path]::GetExtension($InputFile).ToLowerInvariant()
$produced = New-Object System.Collections.Generic.List[string]

function Save-PptxSlides {
  $ppt = $null
  $pres = $null
  try {
    $ppt = New-Object -ComObject PowerPoint.Application
    # PowerPoint refuses WindowState changes while hidden; open read-only + untitled.
    $pres = $ppt.Presentations.Open($InputFile, $true, $true, $false) # ReadOnly, Untitled, WithWindow=false
    $count = [Math]::Min($MaxSlides, $pres.Slides.Count)
    for ($i = 1; $i -le $count; $i++) {
      $out = Join-Path $OutDir ("{0}-slide{1}.png" -f $BaseName, $i)
      # 1600x900 keeps 16:9 decks crisp in the README.
      $pres.Slides.Item($i).Export($out, 'PNG', 1600, 900)
      $produced.Add($out)
      Write-Host "  exported slide $i -> $out"
    }
  }
  finally {
    if ($pres) { try { $pres.Close() } catch {} }
    if ($ppt) { try { $ppt.Quit() } catch {} }
    if ($pres) { [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($pres) }
    if ($ppt) { [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($ppt) }
  }
}

function Save-XlsxSheets {
  $xl = $null
  $wb = $null
  try {
    $xl = New-Object -ComObject Excel.Application
    $xl.Visible = $false
    $xl.DisplayAlerts = $false
    $wb = $xl.Workbooks.Open($InputFile, 0, $true) # UpdateLinks=0, ReadOnly=true

    $xlScreen = 1     # XlPictureAppearance.xlScreen
    $xlBitmap = 2     # XlCopyPictureFormat.xlBitmap
    $exported = 0
    foreach ($ws in $wb.Worksheets) {
      if ($exported -ge $MaxSheets) { break }
      $used = $ws.UsedRange
      if (-not $used -or $used.Rows.Count -lt 1 -or ($used.Rows.Count -eq 1 -and $used.Columns.Count -le 1 -and [string]::IsNullOrWhiteSpace([string]$used.Cells.Item(1,1).Value2))) {
        continue # skip empty sheets
      }
      # Cap the height so multi-hundred-row models remain legible in a README image.
      $rows = [Math]::Min($MaxRows, $used.Rows.Count)
      $cols = $used.Columns.Count
      $firstRow = $used.Row
      $firstCol = $used.Column
      $topLeft = $ws.Cells.Item($firstRow, $firstCol)
      $bottomRight = $ws.Cells.Item($firstRow + $rows - 1, $firstCol + $cols - 1)
      $rng = $ws.Range($topLeft, $bottomRight)

      $rng.CopyPicture($xlScreen, $xlBitmap) | Out-Null
      # Paste the picture into a temporary chart sized to the range, then export it.
      $w = [double]$rng.Width
      $h = [double]$rng.Height
      $chartObj = $ws.ChartObjects().Add(10, 10, $w, $h)
      $chart = $chartObj.Chart
      Start-Sleep -Milliseconds 200
      $chart.Paste()
      $out = Join-Path $OutDir ("{0}-sheet{1}.png" -f $BaseName, ($exported + 1))
      $chart.Export($out, 'PNG') | Out-Null
      $chartObj.Delete() | Out-Null
      $produced.Add($out)
      $exported++
      Write-Host "  exported sheet '$($ws.Name)' -> $out"
    }
    if ($exported -eq 0) { Write-Warning "No non-empty worksheets found in $InputFile" }
  }
  finally {
    if ($wb) { try { $wb.Close($false) } catch {} }
    if ($xl) { try { $xl.Quit() } catch {} }
    if ($wb) { [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($wb) }
    if ($xl) { [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($xl) }
  }
}

Write-Host "Rendering $InputFile ($ext) -> $OutDir"
switch ($ext) {
  '.pptx' { Save-PptxSlides }
  '.ppt'  { Save-PptxSlides }
  '.xlsx' { Save-XlsxSheets }
  '.xlsm' { Save-XlsxSheets }
  '.xls'  { Save-XlsxSheets }
  default { throw "Unsupported extension '$ext' (expected .xlsx or .pptx)" }
}

[System.GC]::Collect()
[System.GC]::WaitForPendingFinalizers()

Write-Host "Produced $($produced.Count) image(s):"
$produced | ForEach-Object { Write-Host "  $_" }
# Emit the produced paths on stdout (one per line) for the orchestrator to consume.
$produced -join "`n"
