# Copilot instructions - FSI Multi-Agent Demo on Microsoft Foundry

This repo is a reusable pattern for building **scenario-based Financial-Services (FSI) agents** on
**Microsoft Foundry hosted agents**, adapting Anthropic's
[`financial-analysis` skills](https://github.com/anthropics/financial-services/tree/main/plugins/vertical-plugins/financial-analysis/skills)
into Foundry skills + toolboxes, fronted by a FastAPI BFF and a Next.js portal on Azure Container Apps.

Use this file to help a user provision and deploy the whole asset end-to-end and get the same result
that is documented in `docs/runbook.md`. Prefer official Microsoft Foundry and Azure documentation and
the vendor repos linked below over guesswork.

All output is AI-generated from public SEC filings and web sources for demo purposes only
(not investment advice). Every scenario analyses a real public company (default: Microsoft/MSFT);
there is no bundled synthetic dataset — agents source figures live from SEC EDGAR + web search.

## Core design principles (do not regress these)

1. **Design agents by scenario, not by skill.** There is exactly one hosted agent per business
   workflow, and each agent reaches its skills through a scenario **toolbox** (skills-over-MCP). Do
   not create one agent per skill. Current roster:
   - `fsi-equity`  -> toolbox `tb-equity-research` (Equity Research & Valuation, `.xlsx`)
   - `fsi-ib-pitch` -> toolbox `tb-ib-pitch` (IB Pitch / Deal Prep, `.pptx`)
   - `fsi-pe-lbo`  -> toolbox `tb-pe-lbo` (PE LBO Screening, `.xlsx`)
2. **Skills are governed Foundry skills**, registered centrally and bound to toolboxes -- never pasted
   into static agent prompts. The hosted runtime consumes them via
   `FoundryToolbox.as_skills_provider()` + `load_skill` (progressive disclosure over MCP).
3. **Route tools through the toolbox via GA Tool Search; keep `code_interpreter` native.**
   `web_search` (bound as `web`) and the SEC EDGAR tools EXECUTE THROUGH each scenario toolbox:
   the runtime opens a SECOND `FoundryToolbox` connection with `load_tools=True` and sets
   `.allowed_tools = {"tool_search", "call_tool"}` post-construction, then puts it in the agent
   `tools` list. Each toolbox declares `toolbox_search_preview` (named `tool_search`), so its
   `tools/list` returns ONLY the `tool_search` + `call_tool` meta-tools; `web` and `sec-edgar___*`
   are DISCOVERED via `tool_search` and executed via `call_tool`, keeping model context flat as the
   toolbox grows. So the toolbox is the single unified, governed, portal-visible tool surface.
   `code_interpreter` is the ONE exception: it comes from `FoundryChatClient` (native) and is NOT
   added to the toolbox, because `ArtifactEgressMiddleware` depends on the native sandbox
   `container_id`. Foundry's GA toolbox docs include Code Interpreter support, but this repo's
   validated download path is the native sandbox, and keeping CI out of the toolbox also stops the
   model shadowing that native path via Tool Search.
   The skills path is unchanged: a first `load_tools=False` toolbox consumed via
   `as_skills_provider()` + `load_skill`, connected with `async with skills_toolbox:` in `main()`
   (skills still surface as MCP resources even with Tool Search on). The preview
   `Foundry-Features: Toolboxes=V1Preview` header is no longer required on GA (harmless if present).
4. **SEC EDGAR is a self-hosted REMOTE MCP tool**, not in-container code. `stefanoamorelli/sec-edgar-mcp`
   runs as its own Container App (built from `agents/mcp/sec-edgar`) and is registered as a GA
   Foundry **project connection** (`azd ai connection create sec-edgar --kind remote-tool
   --auth-type custom-keys`), then attached to each scenario toolbox's `connections`. It is EXECUTED
   THROUGH the toolbox (namespaced `sec-edgar___*` — a DASH, because connection names disallow
   underscores). The Foundry gateway injects a shared-secret header (`x-fsi-mcp-key`). Toggle it
   with the `SEC_EDGAR_MCP_URL` azd env var. Upstream license is AGPL-3.0.

## Repository map

| Path | Purpose |
|---|---|
| `infra/main.bicep` (+ `infra/modules/*`) | Subscription-scoped IaC: RG, Foundry account/project, models, ACR, Storage, Key Vault, App Insights, Container Apps env, RBAC, and API/portal env wiring. |
| `deploy.ps1` | Top-level one-command orchestrator (provision infra -> SEC EDGAR -> map azd env -> skills/connection/toolboxes -> agents -> RBAC -> storage -> api/portal -> validate). `-Skip*` switches for partial re-runs. |
| `scripts/*.ps1` | Deploy helpers: `deploy_sec_edgar.ps1`, `set_azd_env_from_infra.ps1`, `provision_foundry.ps1` (declarative `azd ai` skills + SEC connection + toolboxes), `grant_agent_rbac.ps1`, `ensure_storage_public.ps1`. |
| `scripts/provision_foundry.ps1` | Declarative provisioning: `azd ai skill create` per skill (content fetched at runtime from a pinned Anthropic commit, `-SkillsRef`), `azd ai connection create` for the SEC EDGAR remote-tool connection, and `azd ai toolbox create --from-file` + `publish` for the 3 scenario toolboxes. Replaces the retired REST scripts. |
| `agents/hosted/fsi_hosted_agent.py` | The single env-driven hosted-agent runtime for all 3 scenarios. |
| `agents/hosted/fsi_artifact_egress.py` | `ArtifactEgressMiddleware`: harvests Code Interpreter files and uploads them to the private `artifacts` blob container, appending a `<<<ARTIFACT ...>>>` sentinel. |
| `agents/hosted/_azd/` | `azd ai agent` project. `azure.yaml` declares the 3 services; `agent-src/` is the deployed copy of the runtime. |
| `agents/mcp/sec-edgar/` | Dockerfile + HTTP server for the self-hosted SEC EDGAR remote MCP tool. |
| `api/` | FastAPI BFF: invokes hosted agents (Responses background mode + poll), parses artifact sentinels, strips dead `sandbox:` links, serves `/api/artifacts/{id}`. Config is fail-fast (`PROJECT_ENDPOINT`, `STORAGE_BLOB_ENDPOINT` required). Default prompts target a real public company (MSFT); no bundled dataset — figures come from SEC EDGAR + web search. |
| `portal/` | Next.js branded portal (3 scenario tabs, streaming, artifact download). |
| `scripts/validate.py` | Generic post-deploy validator: runs all 3 scenarios against `API_BASE_URL`, asserts downloadable OOXML. |
| `.env.example` | Canonical reference for every variable, grouped by phase. |
| `docs/runbook.md` | Authoritative operations runbook -- naming, deploy, RBAC, gotchas, teardown. Read this first. |

## End-to-end provision + deploy order

The one-command path is `deploy.ps1`; always read `docs/runbook.md` before acting. It runs
this ordered flow (each step also has a documented manual equivalent in the runbook):

1. **Prerequisites.** `az login`; install `azd` with the `azure.ai.agent` capability; ensure
   Foundry model quota in the target region. Nothing is tied to a specific subscription or
   region — everything derives from `environmentName` (`<env>`) and `location`.
2. **Provision infra** (subscription-scoped Bicep):
   ```powershell
   az deployment sub create --name fsi-<env> --location <location> `
     --template-file infra/main.bicep `
     --parameters environmentName=<env> location=<location> `
                  developerPrincipalId=<your-object-id>
   ```
   A distinct `<env>` gives a fully isolated deployment (RG `rg-<env>`, all resources named
   off it).
3. **(Optional) Deploy the SEC EDGAR MCP Container App** from `agents/mcp/sec-edgar`
   (`scripts/deploy_sec_edgar.ps1`), then set `SEC_EDGAR_MCP_URL` / `FSI_MCP_KEY`.
4. **Map infra outputs into the azd agent env** (`scripts/set_azd_env_from_infra.ps1`).
5. **Provision skills + SEC connection + toolboxes declaratively** against the new project via
   `scripts/provision_foundry.ps1`: `azd ai skill create` per skill, `azd ai connection create`
   for the SEC EDGAR remote-tool connection, `azd ai toolbox create --from-file` + `publish`
   (publish promotes the default version — the MCP endpoint serves the default version).
6. **Deploy the 3 hosted agents** from `agents/hosted/_azd` (see command below).
7. **Grant hosted-agent instance-identity RBAC** (`scripts/grant_agent_rbac.ps1`) and
   **verify storage networking** (`scripts/ensure_storage_public.ps1`; see gotchas).
8. **Build + deploy API and portal images** (`fsi-api`, `fsi-portal`; bake the API URL into
   the portal build). API env vars come from infra outputs.
9. **Validate** (`scripts/validate.py`) and drive the browser portal path.

### Deploy the hosted agents

```powershell
Copy-Item agents/hosted/fsi_hosted_agent.py agents/hosted/_azd/agent-src/ -Force
Copy-Item agents/hosted/fsi_artifact_egress.py agents/hosted/_azd/agent-src/ -Force
# verify the copies match before deploying:
Get-FileHash agents/hosted/fsi_hosted_agent.py, agents/hosted/_azd/agent-src/fsi_hosted_agent.py

cd agents/hosted/_azd
$env:GH_TOKEN = gh auth token; $env:GITHUB_TOKEN = $env:GH_TOKEN
$env:AZD_AGENT_SKIP_ACR = "true"

azd deploy fsi-equity -e <env>
azd deploy fsi-ib-pitch -e <env>
azd deploy fsi-pe-lbo -e <env>
```

## Critical gotchas (these caused real, hard-to-diagnose failures)

- **Storage MUST be network-reachable.** The hosted-agent managed compute and the (VNet-less)
  Container Apps BFF reach Blob Storage over the public endpoint, gated by Entra ID (AAD) RBAC only
  (`allowSharedKeyAccess=false`, `allowBlobPublicAccess=false`). If the storage account has
  `publicNetworkAccess=Disabled` with no private endpoints for BOTH networks, artifact upload fails
  with `AuthorizationFailure "This request is not authorized to perform this operation"` -- the SAME
  message as a missing RBAC role, so it is easy to misdiagnose (all agent instance identities
  already hold the role). Keep `publicNetworkAccess=Enabled` (`infra/modules/storage.bicep` sets
  this explicitly) or add private endpoints for both. **A subscription Azure Policy can flip it back
  to `Disabled` after deploy** (minutes to hours later), silently breaking every artifact download;
  `deploy.ps1` re-asserts it at step 1 and step 7, and you can repair it any time with
  `az storage account update -n <acct> -g <rg> --public-network-access Enabled --default-action Allow`
  or a policy exemption. RBAC alone is not sufficient.
- **Always sync the runtime into `_azd/agent-src` before `azd deploy`.** The source of truth is
  `agents/hosted/*.py`; the deployed copy is `agents/hosted/_azd/agent-src/*.py`. Verify with
  `Get-FileHash`. Deploying a stale copy silently ships old behavior.
- **Two toolbox connections; `code_interpreter` stays native.** The *skills* toolbox
  (`load_tools=False`, `as_skills_provider()`) is connected via `async with skills_toolbox:` in
  `main()`. The *tools* toolbox (`load_tools=True`, `.allowed_tools = {"tool_search", "call_tool"}`)
  goes in the agent `tools` list and routes web_search + SEC EDGAR through the toolbox's GA Tool
  Search (`web` + `sec-edgar___*` are discovered via `tool_search` and run via `call_tool`). This
  sample keeps `code_interpreter` native because artifact egress depends on the hosted sandbox
  `container_id`; run it via `FoundryChatClient.get_code_interpreter_tool()` unless you also
  rework and revalidate artifact delivery.
- **Invoke hosted agents with Responses background mode + poll**, not plain `stream=false`. Plain
  non-streaming holds the connection open and the Foundry gateway disconnects on long Code
  Interpreter tasks. Submit `POST {agentEndpoint}/openai/responses?api-version=v1` with
  `{"input":..., "stream":false, "store":true, "background":true}` (background requires `store:true`),
  then poll `GET .../responses/{id}` until `status=="completed"`. The final text carries the
  `<<<ARTIFACT name=<file> blob=<container>/<path>>>>` sentinel; download the blob privately.
- **The host strips Code Interpreter content types** from the outer `/responses` output
  (`code_interpreter_tool_call` / `_result` are "not supported yet"). You can NEVER detect artifacts
  by scanning the outer response -- they arrive only via the egress middleware sentinel.
- **Framework instrumentation is intentionally disabled in the runtime** (a core-1.10.0 span-attr
  serialization bug crashes on `AutoCodeInterpreterToolParam`). Do not re-enable it without fixing
  that upstream.
- **`azd` credential lookups flake** (`AzureCLICredential` / `AzureDeveloperCLICredential: exit
  status 1`) under back-to-back deploys. Deploy one service at a time and retry a few times.
- **Each hosted agent authenticates as its `instance_identity`** (not the `blueprint` identity).
  Grant `Cognitive Services User` (Foundry account) + `Storage Blob Data Contributor` (storage) to
  the instance identities. To diagnose an identity mismatch, log the storage-token `oid` in the
  container and grant that principal the storage role.

## Validation

- API path: `python scripts/validate.py` (reads `API_BASE_URL`) submits each scenario to
  `/api/run` and asserts a real downloadable OOXML artifact (PK zip). Non-zero exit on any
  failure, so it doubles as a CI gate.
- UI path: drive the portal headlessly -- click a scenario card, submit a prompt, wait for
  `Complete`, download the artifact, and confirm it is valid OOXML.
- Heavy single prompts combining deep SEC retrieval + full multi-sheet DCF can 408-timeout at the
  model layer (~360s); the BFF mitigates with one retry + a corrective artifact turn + a one-sheet
  fallback. That is expected behavior, not a regression.

## Official references

- Microsoft Foundry hosted agents: https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents
- Foundry Agent Service runtime components: https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/runtime-components
- Foundry tools overview: https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/tool-catalog
- Anthropic financial-analysis skills: https://github.com/anthropics/financial-services/tree/main/plugins/vertical-plugins/financial-analysis/skills
- SEC EDGAR MCP: https://github.com/stefanoamorelli/sec-edgar-mcp
