# Local screenshot mechanism

Reproducible capture of the images embedded in the root `README.md`: the **portal UI**
(scenario gallery + a completed run per scenario) and the **rendered artifacts** the
agents produce (`.xlsx` / `.pptx` → PNG). Everything runs against a **live, already
deployed** stack — no Azure SDK, no secrets.

## What it produces

Into `docs/images/` (committed):

| Image | Source |
|---|---|
| `portal-landing.png` | Scenario gallery (one hosted agent per FSI workflow) |
| `portal-run-<scenario>.png` | A completed run: live activity feed, narrative, artifact button |
| `artifact-xlsx-*-sheet1.png` | First worksheet of a generated workbook (Excel COM) |
| `artifact-pptx-*-slide*.png` | First rendered slides of a generated deck (PowerPoint COM) |
| `artifact-equity-dcf.png` | Stable alias for the first rendered Equity workbook image (used by the root README) |
| `artifact-ib-pitch-slide.png` | Stable alias for the best rendered IB deck slide (prefers slide 2, then 3, for the root README) |
| `manifest.json` | Index of images + downloaded artifacts |

Raw downloaded Office files land in `docs/images/_artifacts/` and are **gitignored** —
only the rendered PNGs are committed.

The capture fails fast if a scenario only returns a fallback `*_agent_summary.*` file or
misses an expected default artifact type (for IB pitch: both `.pptx` and `.xlsx`).

## Prerequisites

- **Node.js + npm** — Playwright (Chromium) is installed automatically on first run.
- **Microsoft Excel + PowerPoint** installed locally — used via COM to render the
  workbook/deck to PNG. (No LibreOffice / third-party tools required.) The Office step
  is Windows-only; skip it with `-SkipOffice` on other platforms.

## Usage

From the repo root:

```powershell
./scripts/capture_screenshots.ps1 -PortalUrl https://ca-portal-<env>.<region>.azurecontainerapps.io
```

Common options:

```powershell
# only the landing gallery (fast, no agent run)
./scripts/capture_screenshots.ps1 -PortalUrl <url> -SkipRun

# a specific set of scenarios (keys: equity-research, ib-pitch, pe-lbo)
./scripts/capture_screenshots.ps1 -PortalUrl <url> -Scenarios equity-research,ib-pitch,pe-lbo

# portal screenshots + artifact download, but skip Office rendering
./scripts/capture_screenshots.ps1 -PortalUrl <url> -SkipOffice

# watch the browser; give slow runs more time
./scripts/capture_screenshots.ps1 -PortalUrl <url> -Headed -RunTimeoutSec 900
```

The default scenario set (`equity-research,ib-pitch`) yields one `.xlsx` and one
`.pptx`, enough to showcase both artifact types.

## How it works

1. **`screenshots/capture_portal.mjs`** (Playwright) drives the portal like a user:
   loads the gallery, clicks a scenario card, fills the default Microsoft prompt via the
   preset button, runs the workflow, waits for the real artifact download button
   (`a.artifactChip`) to appear on the **latest** agent card, screenshots the completed
   run, downloads each artifact via the API, and rejects fallback summary files so a
   broken portal run cannot silently refresh the README images. Headless by default, so
   no browser address bar (and therefore no tenant hostname) is captured. Retina scale
   (`deviceScaleFactor: 2`).
2. **`screenshots/capture_office.ps1`** (Office COM) renders each downloaded artifact:
   - `.pptx` → `Slide.Export(...)` native PNG for the first slides; the README alias for IB pitch prefers slide 2, then slide 3, over a text-heavier cover slide.
   - `.xlsx` → copies the used range as a picture, pastes it into a temporary in-sheet
     chart, and `Chart.Export(...)` — a deterministic, clipboard-safe range→PNG path.
3. **`capture_screenshots.ps1`** orchestrates: installs Playwright, runs the portal
   capture, then loops the downloaded artifacts through the Office renderer.

## Notes

- A full run is ~3–6 min per scenario and can be flaky headless; timeouts are generous
  (`-RunTimeoutSec`, default 600s). The script now exits non-zero if any scenario fails
  or only emits fallback summary files, while still preserving any screenshots/artifacts
  it captured for debugging.
- Re-running overwrites PNGs in place, so the README always reflects the latest UI.
- If every scenario suddenly regresses to `*_agent_summary.*`, check the live stack's
  storage account: a governed subscription can flip `publicNetworkAccess` back to
  `Disabled` after deploy. Repair with `scripts/ensure_storage_public.ps1`.
