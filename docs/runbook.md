# FSI Multi-Agent Demo — Runbook

A multi-agent Financial-Services demo on **Azure AI Foundry Agent Service**, adapted
from Anthropic's [`financial-analysis`](https://github.com/anthropics/financial-services)
plugin skills, fronted by a branded web portal on Azure Container Apps. It illustrates
the **top 3 FSI analyst workflows**:

| # | Scenario | Specialist agents (in order) | Orchestrator | Deliverable |
|---|----------|------------------------------|--------------|-------------|
| S1 | **Equity Research & Valuation** | 3-Statement → DCF → Comps | `fsi-orchestrator-equity-research` | `.xlsx` valuation package |
| S2 | **Investment Banking Pitch** | Competitive-Analysis → PPTX-Author → Deck-QC | `fsi-orchestrator-ib-pitch` | `.pptx` pitch deck |
| S3 | **Private Equity LBO Screening** | LBO → Model-Audit | `fsi-orchestrator-pe-lbo` | `.xlsx` LBO model |

All numbers are **synthetic** (fictional company *NovaGrid Technologies* + fictional
peers). Nothing here is investment advice.

---

## 1. Live endpoints

| Resource | Value |
|----------|-------|
| **Portal (demo this)** | https://ca-portal-fsi-demo.orangecoast-891e69ba.eastus2.azurecontainerapps.io |
| API | https://ca-api-fsi-demo.orangecoast-891e69ba.eastus2.azurecontainerapps.io |
| Foundry project endpoint | https://aif66lhnuec.services.ai.azure.com/api/projects/proj-fsi-demo |
| Resource group | `rg-fsi-demo` (region `eastus2`) |
| Subscription | `6a7da9fe-e881-4d1a-bf1b-c5f4fc3530ac` |

Supporting resources in `rg-fsi-demo`: Foundry account `aif66lhnuec` + project
`proj-fsi-demo`, ACR `acr66lhnuec`, Storage `st66lhnuec`, Key Vault
`kv-fsi-demo-66lhnuec`, Application Insights + Log Analytics, user-assigned managed
identity `id-fsi-demo-66lhnuec` (clientId `ba586a91-c354-4af8-91fb-8da826296ccf`).

---

## 2. Architecture at a glance

```
Browser ─▶ Portal (Next.js, Container App)
             │  fetch /api/run (SSE)
             ▼
          API (FastAPI, Container App)  ──OpenTelemetry──▶ Application Insights
             │  orchestrator-worker pipeline
             ▼
   Azure AI Foundry Agent Service  (project proj-fsi-demo)
     ├─ 8 specialist prompt agents  (code_interpreter + web_search)
     ├─ 3 orchestrator prompt agents (final synthesis)
     └─ 3 toolboxes  (tb-equity-research / tb-ib-pitch / tb-pe-lbo)
            exposed as MCP servers — the shared, portal-visible tool catalog
```

- **Agents** are Foundry *prompt agents*; instructions are the converted Anthropic
  SKILL.md files (`agents/skills/*.md`). The tool surface (`code_interpreter` for
  formula-driven `.xlsx`/`.pptx`, `web_search` for grounding) is attached directly to
  each agent — the GA, reliable path.
- **Toolboxes** are the reusable Foundry *tool catalog* (`code_interpreter` + `web`),
  created via REST and surfaced as live MCP servers. The portal lists them under each
  scenario so the "shared tools" story is visible and demonstrable.
- **Orchestration** runs in the backend (orchestrator-worker) for reliability and
  traceability: specialists run in order, their outputs feed downstream, then the
  scenario orchestrator agent synthesizes the final package.
- **Synthetic data** is injected in-context (code_interpreter has no internet); the
  datasets also live in Storage (`datasets` container) and `api/data/`.

---

## 3. Demo script (≈ 5 min)

1. Open the **portal URL**. The landing view shows the three scenario cards and, per
   scenario, the backing **toolbox** and its tools.
2. Pick **Equity Research & Valuation** → **Run** (or edit the prompt first).
3. Watch the live stream: each specialist agent starts, streams its reasoning, and
   emits artifacts. You'll see `3-Statement → DCF → Comps → Orchestrator synthesis`.
4. When it finishes, **download the generated `.xlsx`** and open it — the cells are
   real formulas, not hard-coded values (formulas-over-hardcodes principle).
5. Repeat for **IB Pitch** (produces a downloadable `.pptx` deck) and **PE LBO**
   (produces an LBO workbook with IRR/MOIC).
6. Optional: show **Application Insights → Transaction search / Application map** to
   see one span per scenario and per agent turn (attributes: agent, role, deltas,
   artifacts, ok).

**Validated end-to-end (cloud):**

| Scenario | Agents run | Artifacts |
|----------|-----------|-----------|
| equity-research | 3 specialists + orchestrator | `NovaGrid_DCF_Model_*.xlsx` |
| ib-pitch | 3 specialists + orchestrator | `NovaGrid_IB_Pitch_*.pptx`, `novagrid_pitch_deck.pptx` |
| pe-lbo | 2 specialists + orchestrator | LBO workbook `.xlsx` (+ audit) |

---

## 4. Observability

- The API is instrumented with `azure-monitor-opentelemetry`
  (`api/app/telemetry.py`). It configures Azure Monitor when
  `APPLICATIONINSIGHTS_CONNECTION_STRING` is set on the container app, auto-instruments
  FastAPI/HTTP, and the orchestrator emits custom spans:
  - `scenario.run` — attributes `fsi.scenario`, `fsi.toolbox`.
  - `agent.turn` — attributes `fsi.agent`, `fsi.role`, `fsi.deltas`, `fsi.artifacts`,
    `fsi.chars`, `fsi.ok`.
- Confirm telemetry is on: `GET /api/health` returns `"telemetry": true`.
- Query in App Insights (Logs):
  ```kusto
  dependencies
  | where timestamp > ago(1h) and name in ("scenario.run","agent.turn")
  | project timestamp, name, customDimensions
  | order by timestamp desc
  ```

---

## 5. Operating the demo

### Re-run a scenario headlessly
```powershell
$py = @'
import json, urllib.request
API="https://ca-api-fsi-demo.orangecoast-891e69ba.eastus2.azurecontainerapps.io"
req=urllib.request.Request(API+"/api/run",
    data=json.dumps({"scenario":"pe-lbo"}).encode(),
    headers={"Content-Type":"application/json"})
with urllib.request.urlopen(req,timeout=1200) as r:
    for line in r:
        s=line.decode().strip()
        if s.startswith("data:"): print(s[5:])
'@
$py | & .\agents\.venv\Scripts\python.exe -
```
Valid scenarios: `equity-research`, `ib-pitch`, `pe-lbo`.

### Rebuild + redeploy the API
```powershell
$env:PYTHONUTF8='1'; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8
az acr build --registry acr66lhnuec --image fsi-api:latest ./api
# NOTE: the CLI may crash with a cosmetic UnicodeEncodeError on Windows while
# streaming logs — the build still succeeds. Verify:
az acr task list-runs --registry acr66lhnuec --top 1 --query "[0].status" -o tsv
az containerapp update -n ca-api-fsi-demo -g rg-fsi-demo --revision-suffix v$(Get-Date -Format 'MMddHHmmss')
```

### Rebuild + redeploy the portal
`NEXT_PUBLIC_*` vars are build-time inlined, so the API URL is baked at build:
```powershell
az acr build --registry acr66lhnuec --image fsi-portal:latest `
  --build-arg NEXT_PUBLIC_API_BASE_URL=https://ca-api-fsi-demo.orangecoast-891e69ba.eastus2.azurecontainerapps.io `
  ./portal
az containerapp update -n ca-portal-fsi-demo -g rg-fsi-demo --revision-suffix v$(Get-Date -Format 'MMddHHmmss')
```

### Recreate agents / toolboxes
```powershell
cd agents
.\.venv\Scripts\python.exe scripts\create_toolboxes.py   # idempotent
.\.venv\Scripts\python.exe scripts\create_agents.py       # writes definitions/agents-manifest.json
```

---

## 6. Plugging in real vendor data (from synthetic → live)

Today the agents run on **synthetic** NovaGrid data + `web_search` grounding. The
Anthropic plugin references vendor MCP sources (FactSet, S&P Kensho, Daloopa, Moody's,
Morningstar, PitchBook, LSEG, Aiera, MT Newswire, Chronograph, Egnyte, Box). To go live:

1. Store each vendor's API key/endpoint in Key Vault `kv-fsi-demo-66lhnuec`.
2. Add the vendor as an MCP tool connection on the Foundry project (or add an
   `mcp` tool entry to the relevant toolbox version) referencing the Key Vault secret.
3. Attach the tool to the specialist agent(s) that need it (e.g. Comps/DCF → FactSet;
   Competitive-Analysis → PitchBook).
4. Update the specialist instructions to prefer the live source over the injected
   synthetic dataset.

No portal or orchestration changes are required — the toolbox/agent tool surface is
the only thing that changes.

---

## 7. Teardown

Everything lives in one resource group, so cleanup is a single command:
```powershell
az group delete --name rg-fsi-demo --yes --no-wait
```
This removes the Foundry account/project (and all agents + toolboxes), both container
apps, ACR, Storage, Key Vault, App Insights, and the managed identity.

---

## 8. Key gotchas (captured during build)

- **Toolbox creation:** `azd ai toolbox create` drops `api-version=v1` + the
  `Foundry-Features: Toolboxes=V1Preview` header → `WorkspaceNotFound`. Use direct REST
  `POST {PE}/toolboxes/{name}/versions?api-version=v1` (see `create_toolboxes.py`).
- **Agent invoke shape:** `responses.create(..., extra_body={"agent_reference":
  {"name": <n>, "type": "agent_reference"}})`. The older `{"agent": ...}` shape is
  rejected as deprecated; `type` is required.
- **Managed identity in Container Apps:** set `AZURE_CLIENT_ID` to the user-assigned
  identity's clientId, or `DefaultAzureCredential` fails.
- **SSE + ingress idle timeout:** stream deltas incrementally and emit keepalive
  comments during long tool execution, or Container Apps ingress drops the connection.
- **Model policy (env date 2026-07-06):** GPT-4 family is blocked; deployments are
  `gpt-5.1`, `gpt-5.4-mini`, `text-embedding-3-large`. Per-model capacity was raised to
  150 TPM to support the multi-agent pipeline; the backend retries on 429.
- **`az acr build` on Windows:** cosmetic `UnicodeEncodeError` while streaming logs —
  the build still succeeds; verify with `az acr task list-runs`.
