# FSI Multi-Agent Demo v3 - Runbook

This runbook operates the **v3** deployment of the FSI multi-agent demo on Azure AI Foundry. v3 runs in a separate resource group, `rg-fsi-demo-v3`, and leaves the earlier `rg-fsi-demo` and `rg-fsi-demo-v2` deployments untouched.

The design follows Anthropic's [`financial-analysis`](https://github.com/anthropics/financial-services/tree/main/plugins/vertical-plugins/financial-analysis/skills) skill layout, but deploys the runtime as three scenario-based **Azure AI Foundry hosted agents**.

All data is synthetic and for demo purposes only.

## 1. Live endpoints and resources

| Resource | Value |
|---|---|
| Portal | https://ca-portal-fsi-demo-v3.politeocean-8e501b06.eastus2.azurecontainerapps.io |
| API | https://ca-api-fsi-demo-v3.politeocean-8e501b06.eastus2.azurecontainerapps.io |
| Resource group | `rg-fsi-demo-v3` |
| Region | `eastus2` |
| Subscription | `6a7da9fe-e881-4d1a-bf1b-c5f4fc3530ac` |
| Foundry project endpoint | `https://aifxzqm33pk.services.ai.azure.com/api/projects/proj-fsi-demo-v3` |
| Foundry account / project | `aifxzqm33pk` / `proj-fsi-demo-v3` |
| ACR | `acrxzqm33pk` |
| Storage | `stxzqm33pk` |
| Artifacts container | `artifacts` |
| Key Vault | `kv-fsi-demo-v3-xzqm33pk` |

## 2. Scenario map

| Scenario key | Hosted agent | Toolbox | Deliverable |
|---|---|---|---|
| `equity-research` | `fsi-equity-v3` | `tb-equity-research` | Valuation `.xlsx`; SEC filing-backed public-company metrics when a real ticker is supplied |
| `ib-pitch` | `fsi-ib-pitch-v3` | `tb-ib-pitch` | Pitch `.pptx`; SEC 10-K/10-Q/8-K context for public issuers |
| `pe-lbo` | `fsi-pe-lbo-v3` | `tb-pe-lbo` | LBO `.xlsx`; SEC financials for public LBO targets where applicable |

The hosted agents are deployed through `azd ai agent` from:

```text
C:\Users\samsonlee\GHCP\fsi-multiagent-demo\agents\hosted\_azd
```

Each service block in `azure.yaml` points at the same Python source and differs by environment variables:

- `FSI_SCENARIO_KEY`
- `FSI_SCENARIO_NAME`
- `TOOLBOX_ENDPOINT`
- `AGENT_NAME`

## 3. Architecture at a glance

```text
Browser
  -> Portal Container App (Next.js)
  -> API Container App (FastAPI BFF, SSE)
  -> Foundry Responses protocol, background:true + poll
  -> Scenario hosted agent in Foundry Agent Service
  -> Foundry toolbox MCP for skill loading
  -> SEC EDGAR MCP-backed function tools for public filings and XBRL
  -> Native code_interpreter / web_search
  -> ArtifactEgressMiddleware uploads generated files to Blob Storage
  -> API reads private blob and exposes /api/artifacts/{id}
```

Important runtime decisions:

- The API does **not** run an in-process multi-agent framework anymore.
- The API does **not** call 8 specialist prompt agents plus 3 orchestrators anymore.
- The API invokes exactly one deployed hosted agent per scenario.
- Skills are loaded through toolbox MCP with `FoundryToolbox.as_skills_provider()`.
- SEC EDGAR is exposed as a narrow in-container function-tool surface backed by the
  open-source `sec-edgar-mcp` package. It is not exposed as a public unauthenticated
  HTTP MCP endpoint.
- Code Interpreter is native, not toolbox-provided, because the toolbox preview Code Interpreter path returned server-side 500s during validation.
- Long-running hosted-agent calls use Responses background mode. Plain non-streaming calls can be disconnected by the gateway before Code Interpreter finishes.

## 4. Demo script

1. Open the portal.
2. Show the three scenario cards and the visible toolbox/skill metadata.
3. Run **Equity Research and Valuation**. The hosted agent should produce a downloadable valuation workbook.
4. Run **Private Equity LBO Screening**. The hosted agent should produce a downloadable LBO workbook.
5. Run **Investment Banking Pitch**. The hosted agent should produce a downloadable pitch deck.
6. Optional public-filing path: edit a prompt to include a real public ticker, for example "Use AAPL SEC filings for the public-company benchmark context." The agent should prefer SEC EDGAR tools for filing metadata, sections, XBRL financials, and cite SEC URLs.
7. Open one downloaded file to show it is a real Office artifact, not a text placeholder.
8. Optionally show the API health endpoint and Application Insights telemetry.

Validated live artifacts:

| Scenario | Validated artifact |
|---|---|
| `equity-research` | `NovaGrid_valuation_snapshot.xlsx` |
| `pe-lbo` | `NovaGrid_Compact_LBO.xlsx` |
| `ib-pitch` | `NovaGrid_pitch_demo.pptx` |
| SEC EDGAR smoke via `equity-research` | `sec_smoketest_aapl.xlsx` with AAPL CIK, latest 10-K date, accession number, and SEC URL |

## 5. API checks

Health:

```powershell
Invoke-RestMethod https://ca-api-fsi-demo-v3.politeocean-8e501b06.eastus2.azurecontainerapps.io/api/health
```

Expected:

```json
{
  "status": "ok",
  "telemetry": true
}
```

Scenario catalog:

```powershell
Invoke-RestMethod https://ca-api-fsi-demo-v3.politeocean-8e501b06.eastus2.azurecontainerapps.io/api/scenarios
```

Toolbox catalog:

```powershell
Invoke-RestMethod https://ca-api-fsi-demo-v3.politeocean-8e501b06.eastus2.azurecontainerapps.io/api/toolboxes
```

## 6. Headless validation

The session validator submits to `/api/run`, reads the SSE stream, captures artifact URLs, downloads files, and checks that Office artifacts start with the expected OOXML zip signature.

```powershell
cd C:\Users\samsonlee\.copilot\session-state\c9ab9ad2-4089-49e2-a82c-0738e120c0a2\files

python .\v3_api_e2e.py equity-research "Create a compact valuation workbook for NovaGrid."
python .\v3_api_e2e.py pe-lbo "Create a compact sponsor LBO screen for NovaGrid."
python .\v3_api_e2e.py ib-pitch "Create a concise buyer pitch deck for NovaGrid."
```

## 7. Rebuild and redeploy hosted agents

When changing hosted-agent code, copy the source into the azd agent source folder before deployment.

```powershell
cd C:\Users\samsonlee\GHCP\fsi-multiagent-demo\agents\hosted

Copy-Item .\fsi_hosted_agent_v3.py .\_azd\agent-src\fsi_hosted_agent_v3.py -Force
Copy-Item .\fsi_artifact_egress.py .\_azd\agent-src\fsi_artifact_egress.py -Force

cd .\_azd
$env:GH_TOKEN = gh auth token
$env:GITHUB_TOKEN = $env:GH_TOKEN
$env:AZD_AGENT_SKIP_ACR = "true"
azd env set SEC_EDGAR_USER_AGENT "Your Name (your.email@example.com)"

azd deploy fsi-equity-v3
azd deploy fsi-ib-pitch-v3
azd deploy fsi-pe-lbo-v3
```

Deployment notes:

- `azd config set auth.useAzCliAuth true` should remain enabled.
- `azure.yaml` environment variables must use list form: `{ name, value }`.
- Keep `ENABLE_INSTRUMENTATION=false` in hosted-agent service env vars.
- Set `SEC_EDGAR_USER_AGENT` before deployment. The SEC requires a real contact name and email for automated EDGAR access, and `sec-edgar-mcp` fails startup/tool calls without it.
- If `AzureCLICredential` fails transiently during deploy, refresh Azure CLI auth with `az account get-access-token` and retry.

## 8. Required RBAC

The API Container App user-assigned managed identity needs:

| Scope | Role |
|---|---|
| Foundry account `aifxzqm33pk` | `Cognitive Services User` |
| Foundry account/project | `Foundry User` |
| Storage `stxzqm33pk` | `Storage Blob Data Contributor` |
| ACR `acrxzqm33pk` | `AcrPull` |
| Key Vault | `Key Vault Secrets User` |

Each hosted agent has its own instance identity. Each agent identity needs:

| Scope | Role |
|---|---|
| Foundry account `aifxzqm33pk` | `Cognitive Services User` |
| Storage `stxzqm33pk` | `Storage Blob Data Contributor` |

Known hosted-agent principal IDs from validation:

| Agent | Instance principal ID |
|---|---|
| `fsi-equity-v3` | `40528086-0db5-45e6-a789-7ef3ed41c466` |
| `fsi-ib-pitch-v3` | `bef11ccf-4f87-48bf-8d4e-c4ebdfd7dfcd` |
| `fsi-pe-lbo-v3` | `3649c131-e8e0-4a33-98b0-05cb62124ebf` |

> **Identity note:** a Foundry hosted agent exposes an `instance_identity` (per-agent, granted the
> roles above) and a per-agent `blueprint` identity. In this deployment the in-container
> `DefaultAzureCredential` used by the egress middleware resolves to the **instance identity**
> (confirmed via the token `oid`), which is why granting the instance identities the storage role is
> what matters. If a future runtime resolves to a different identity, the fastest diagnosis is to log
> the storage-token `oid` in the container and grant that principal `Storage Blob Data Contributor`.
> **Reminder:** RBAC alone is not sufficient -- the storage account must also be network-reachable
> (`publicNetworkAccess=Enabled`); see the storage gotcha in section 12.

## 9. Rebuild and redeploy the API

```powershell
cd C:\Users\samsonlee\GHCP\fsi-multiagent-demo
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

az acr build --registry acrxzqm33pk --image fsi-api:v3 .\api
az acr task list-runs --registry acrxzqm33pk --top 1 --query "[0].status" -o tsv

az containerapp update `
  -n ca-api-fsi-demo-v3 `
  -g rg-fsi-demo-v3 `
  --image acrxzqm33pk.azurecr.io/fsi-api:v3 `
  --revision-suffix v$(Get-Date -Format 'MMddHHmmss')
```

If the Windows console raises `UnicodeEncodeError` while streaming `az acr build` logs, verify the run server-side with `az acr task list-runs`; the build can still succeed.

## 10. Rebuild and redeploy the portal

`NEXT_PUBLIC_API_BASE_URL` is compiled into the Next.js image.

```powershell
cd C:\Users\samsonlee\GHCP\fsi-multiagent-demo

az acr build --registry acrxzqm33pk --image fsi-portal:v3 `
  --build-arg NEXT_PUBLIC_API_BASE_URL=https://ca-api-fsi-demo-v3.politeocean-8e501b06.eastus2.azurecontainerapps.io `
  .\portal

az containerapp update `
  -n ca-portal-fsi-demo-v3 `
  -g rg-fsi-demo-v3 `
  --image acrxzqm33pk.azurecr.io/fsi-portal:v3 `
  --revision-suffix v$(Get-Date -Format 'MMddHHmmss')
```

## 11. Observability

The API uses `azure-monitor-opentelemetry` in `api\app\telemetry.py`. Telemetry is enabled when `APPLICATIONINSIGHTS_CONNECTION_STRING` is present on the API Container App.

Useful query:

```kusto
dependencies
| where timestamp > ago(1h)
| where name in ("scenario.run", "foundry.background.submit", "foundry.background.poll")
| project timestamp, name, customDimensions
| order by timestamp desc
```

Hosted-agent framework instrumentation is disabled in `fsi_hosted_agent_v3.py` with `agent_framework.observability.disable_instrumentation()`. This is intentional: the deployed host can otherwise re-enable instrumentation and hit a serialization issue for native Code Interpreter tool parameters.

## 12. Known gotchas

- **Tool approval:** `SkillsProvider` creates skill tools with approval required. The deployed hosted-agent runtime does not provide an `AgentSession`, so `ToolApprovalMiddleware` fails. The code patches the provider's created tools to `approval_mode=None`.
- **Responses background mode:** use `background:true` with `store:true`; otherwise long non-streaming requests can disconnect before Code Interpreter completes.
- **Toolbox Code Interpreter:** toolbox MCP Code Interpreter returned server-side 500s during validation, so v3 uses native Code Interpreter from the project client.
- **SEC EDGAR MCP (remote tool):** `stefanoamorelli/sec-edgar-mcp` runs as a **self-hosted Container App** (`ca-secedgar-mcp-*`) and is consumed by the hosted agents as a **Foundry-native remote (hosted) MCP tool** built from the project client, NOT imported in the agent image. The Foundry gateway connects to it and injects a shared-secret header (`x-fsi-mcp-key`), so the MCP endpoint is not publicly usable without the key. It still requires `SEC_EDGAR_USER_AGENT` on the MCP Container App and should obey SEC fair-access limits. Set/clear `SEC_EDGAR_MCP_URL` (azd env) to enable/disable the tool.
- **SEC EDGAR license:** the upstream `sec-edgar-mcp` package is AGPL-3.0. This is acceptable for an internal demo, but review licensing before commercial redistribution or embedding in proprietary distributions.
- **Storage MUST be network-reachable (critical):** the hosted-agent managed compute and the (VNet-less) Container Apps BFF reach Blob Storage over the public endpoint, gated by Entra ID (AAD) RBAC only (`allowSharedKeyAccess=false`, `allowBlobPublicAccess=false`). If the storage account is set to `publicNetworkAccess=Disabled` (with no private endpoints/VNet rules for both the agent compute and the Container Apps env), artifact **upload fails with `AuthorizationFailure` "This request is not authorized to perform this operation"** even though RBAC is correct -- Azure Storage returns that generic error for network denial. Keep `publicNetworkAccess=Enabled` (`infra/modules/storage.bicep` sets this explicitly) or add private endpoints for both networks. Diagnose via the deployed session logstream: look for `fsi.hosted.v3.egress:blob upload ... failed`.
- **Artifact egress:** hosted-agent HTTP responses do not reliably expose Code Interpreter file citations to the BFF (the host also drops `code_interpreter_tool_call`/`_result` content from the outer response). `ArtifactEgressMiddleware` therefore harvests files in-container off the CI `container_id` (present on the response object), uploads to the private `artifacts` Blob container, and appends a parseable `<<<ARTIFACT name=... blob=...>>>` sentinel to the response text. The BFF must invoke with `stream=False` (background mode) for the middleware to observe the fully materialised response.
- **Artifact fallback:** if a hosted run completes but no Blob sentinel appears after one corrective artifact turn, the API registers a valid one-sheet `.xlsx` summary artifact from the agent narrative so the portal still has a downloadable file. Treat this as a demo-resilience fallback, not a replacement for the primary Code Interpreter artifact path.
- **Transient model errors:** the API retries one primary hosted-agent run for 408 timeouts or 429 rate limits before surfacing an error.
- **Container App identity:** set `AZURE_CLIENT_ID` so `DefaultAzureCredential` selects the app UAMI.
- **Older deployments:** do not use old v1 resource names such as `rg-fsi-demo`, `aif66lhnuec`, or `acr66lhnuec` when operating v3.

## 13. Rebuild from Anthropic skills as a reusable repo

This repo is intended to be reusable for future FSI agent builds, not only this single deployment.

1. **Start with the source skill catalog.** Use Anthropic's `financial-analysis/skills` folder as the upstream pattern. Keep a pinned copy of each selected `SKILL.md` under `agents\skills` and document any Azure-specific adaptation.
2. **Design by scenario.** Do not create one long-lived agent per skill. Create one hosted agent per business workflow, then assign it a scenario toolbox. In this demo, `fsi-equity-v3` uses `tb-equity-research`, `fsi-ib-pitch-v3` uses `tb-ib-pitch`, and `fsi-pe-lbo-v3` uses `tb-pe-lbo`.
3. **Register skills.** Run `agents\scripts\provision_skills.py` against the target Foundry project to create centrally managed Foundry skills.
4. **Bind and promote toolboxes.** Run `agents\scripts\bind_skills_to_toolboxes.py`. Confirm the toolbox default version points to the version containing the skill references; the MCP endpoint serves the default version.
5. **Deploy hosted agents.** Use the env-driven runtime in `agents\hosted\fsi_hosted_agent_v3.py`. Keep the `_azd\agent-src` copy synced before `azd deploy`.
6. **Use native tools where the toolbox preview is unreliable.** The runtime consumes skills through `FoundryToolbox.as_skills_provider()`, but uses native Foundry `code_interpreter` and `web_search`. Toolbox MCP Code Interpreter returned preview server-side 500s during validation.
7. **Integrate SEC EDGAR safely.** `sec-edgar-mcp` runs as a self-hosted Container App (built from `agents\mcp\sec-edgar`) and is attached to the agents as a Foundry-native remote MCP tool; the gateway injects a shared-secret header (`x-fsi-mcp-key`), so the endpoint is unusable without the key and no SEC code ships in the agent image. Toggle it with the `SEC_EDGAR_MCP_URL` azd env var. Set `SEC_EDGAR_USER_AGENT` to a real contact, keep `SEC_EDGAR_DEEP_TOOLS=false` by default, and review the upstream AGPL-3.0 license before commercial reuse.
8. **Keep the BFF resilient.** The API uses Responses background mode, retries one transient 408/429 hosted-agent run, issues one artifact-corrective turn when no Blob sentinel is returned, and finally creates a valid summary `.xlsx` fallback if hosted artifact egress still misses. This keeps the UI demo usable while preserving the hosted agent as the analysis source.
9. **Validate both paths.** Run the headless API validator and then a browser-driven portal test. The UI validation must click a scenario card, submit a prompt, wait for `Complete`, and download a valid OOXML artifact.

Official references:

- Azure AI Foundry hosted agents: https://learn.microsoft.com/azure/ai-foundry/agents/concepts/hosted-agents?view=foundry
- Azure AI Foundry Agent Service runtime components: https://learn.microsoft.com/azure/ai-foundry/agents/concepts/runtime-components?view=foundry
- Azure AI Foundry tools overview: https://learn.microsoft.com/azure/ai-foundry/agents/how-to/tools/overview?view=foundry
- Anthropic financial-analysis skills: https://github.com/anthropics/financial-services/tree/main/plugins/vertical-plugins/financial-analysis/skills
- SEC EDGAR MCP package: https://github.com/stefanoamorelli/sec-edgar-mcp

## 14. Moving from synthetic data to live vendor data

To connect live FSI data providers:

1. Store vendor endpoints and secrets in Key Vault.
2. Register vendor MCP tools or toolbox tools in the Foundry project.
3. Expose each tool only through the scenario toolbox that needs it.
4. Update the corresponding skill instructions to prefer live data over synthetic data.

Candidate mappings:

| Workflow need | Vendor MCP examples |
|---|---|
| Public-company filings and XBRL financials | SEC EDGAR via `sec-edgar-mcp` |
| Market data, estimates, comps beyond filings | FactSet, LSEG, Morningstar |
| Private-company and sponsor data | PitchBook, Chronograph |
| Credit and issuer context | Moody's |
| Transcripts and news | Aiera, MT Newswire |
| Source documents | Box, Egnyte |

## 15. Teardown

Delete only the v3 resource group when retiring this iteration:

```powershell
az group delete --name rg-fsi-demo-v3 --yes --no-wait
```

Do not delete `rg-fsi-demo` or `rg-fsi-demo-v2` unless intentionally retiring the earlier live versions.
