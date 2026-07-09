# FSI Multi-Agent Demo — Operations Runbook

This runbook explains how the stack is deployed and operated. The one-command path is
[`deploy.ps1`](../deploy.ps1); the sections below document what each step does so you can
run, debug, or re-run parts of it manually.

Everything is parameterized by `environmentName` (referred to below as `<env>`). All
resource names derive from it, so a second `<env>` gives a fully isolated deployment. No
resource names, endpoints, principal IDs, or local paths are hardcoded.

All output is AI-generated from public SEC filings and web sources for demo purposes only
(not investment advice). The default one-click prompts analyse Microsoft (MSFT); edit the
mandate to target any public ticker.

## 1. Naming convention

| Resource | Name pattern |
|---|---|
| Resource group | `rg-<env>` |
| Foundry account | `aif<token>` |
| Foundry project | `proj-<env>` |
| Storage account | `st<token>` |
| Container Registry | `acr<token>` |
| Key Vault | `kv-<env>-<token>` |
| App Insights / Log Analytics | `appi-<env>-<token>` / `log-<env>-<token>` |
| Container Apps env | `cae-<env>-<token>` |
| API / Portal apps | `ca-api-<env>` / `ca-portal-<env>` |
| Hosted agents | `fsi-equity`, `fsi-ib-pitch`, `fsi-pe-lbo` |
| Toolboxes | `tb-equity-research`, `tb-ib-pitch`, `tb-pe-lbo` |

`<token>` is a deterministic `uniqueString` hash of the subscription + env + location.

## 2. Prerequisites

- Azure subscription with Foundry model quota in the target region for the deployments in the
  `modelDeployments` param (default: a single `gpt-5.4` GlobalStandard deployment at 150K TPM).
  Add models or change capacity/region via that param to fit your quota; keep
  `agentModelDeploymentName` in the list.
- `az`, `azd` (with the Foundry agents extension — install with
  `azd extension install azure.ai.agents`, verify with `azd ai agent --help`),
  `gh` (authenticated), Python 3.11+.
- `az login`; `pip install -r agents/scripts/requirements.txt` (pinned deps for the
  provisioning scripts). Multi-subscription users: `az account set --subscription <id>`
  or pass `-SubscriptionId <id>` to `deploy.ps1`.
- `deploy.ps1` auto-derives your object ID via `az ad signed-in-user show`; pass `-PrincipalId`
  only to override (or when running the raw `az deployment` path with `developerPrincipalId`).
- The infra step prints benign warnings you can ignore: a Bicep upgrade notice and
  `BCP081` "resource type does not have types available" for the preview
  `Microsoft.CognitiveServices` API version — these do not block deployment.

## 3. End-to-end deploy (what `deploy.ps1` does)

1. **Provision infra** (subscription-scoped bicep). Creates the resource group and all
   resources above plus model deployments and app-identity RBAC.
   ```powershell
   az deployment sub create --name fsi-<env> --location <location> `
     --template-file infra/main.bicep `
     --parameters environmentName=<env> location=<location> `
                  developerPrincipalId=<your-object-id> `
                  agentModelDeploymentName=gpt-5.4
   ```
2. **Register skills + create toolboxes** against the new project (uses `PROJECT_ENDPOINT`
   from the infra `AZURE_AI_PROJECT_ENDPOINT` output):
   ```powershell
   $env:PROJECT_ENDPOINT = "<AZURE_AI_PROJECT_ENDPOINT>"
   python agents/scripts/provision_skills.py
   python agents/scripts/create_toolboxes.py
   ```
3. **(Optional) Deploy the SEC EDGAR MCP** Container App and generate its shared secret:
   ```powershell
   ./scripts/deploy_sec_edgar.ps1 -ResourceGroup rg-<env> -RegistryName acr<token> `
     -UserAssignedIdentityId <AZURE_MANAGED_IDENTITY_ID> `
     -SecEdgarUserAgent "Your Name (you@example.com)" -FsiMcpKey <generated-secret>
   ```
4. **Bind skills (+ SEC tool) to toolboxes** and promote the default version (the MCP
   endpoint serves the default version):
   ```powershell
   # With SEC_EDGAR_MCP_URL / FSI_MCP_KEY set if SEC EDGAR is enabled:
   python agents/scripts/bind_skills_to_toolboxes.py
   ```
5. **Map infra outputs into the azd agent environment**:
   ```powershell
   ./scripts/set_azd_env_from_infra.ps1 -ProjectEndpoint <...> -StorageBlobEndpoint <...> `
     -ModelDeploymentName gpt-5.4 -EnvName <env>
   ```
6. **Deploy the 3 hosted agents** (see §4).
7. **Grant agent instance-identity RBAC** (see §5).
8. **Build + deploy API and portal** images (see §6).
9. **Validate** all three scenarios (see §7).

## 4. Deploy / redeploy the hosted agents

The hosted-agent module (`agents/hosted/*.py`) is the source of truth; the deployed copy is
`agents/hosted/_azd/agent-src/*.py`. Always sync before deploying — a stale copy silently
ships old behavior.

```powershell
Copy-Item agents/hosted/fsi_hosted_agent_v3.py agents/hosted/_azd/agent-src/ -Force
Copy-Item agents/hosted/fsi_artifact_egress.py agents/hosted/_azd/agent-src/ -Force
Copy-Item agents/hosted/requirements.txt       agents/hosted/_azd/agent-src/ -Force
Get-FileHash agents/hosted/fsi_hosted_agent_v3.py, agents/hosted/_azd/agent-src/fsi_hosted_agent_v3.py

cd agents/hosted/_azd
$env:GH_TOKEN = gh auth token; $env:GITHUB_TOKEN = $env:GH_TOKEN
$env:AZD_AGENT_SKIP_ACR = "true"
azd deploy fsi-equity -e <env>
azd deploy fsi-ib-pitch -e <env>
azd deploy fsi-pe-lbo -e <env>
```

Notes:
- `azd config set auth.useAzCliAuth true` should be enabled.
- `azure.yaml` environment variables use list form (`{ name, value }`) and resolve from the
  azd environment set in step 5.
- Keep `ENABLE_INSTRUMENTATION=false` on the agent services.

## 5. Required RBAC

The bicep grants the **app** managed identity its roles automatically. The **hosted-agent
instance identities** exist only after the agents deploy, so `deploy.ps1` grants them via
`scripts/grant_agent_rbac.ps1` (or run it manually).

App managed identity:

| Scope | Role |
|---|---|
| Foundry account | `Cognitive Services User`, `Foundry User` |
| Storage account | `Storage Blob Data Contributor` |
| Container Registry | `AcrPull` |
| Key Vault | `Key Vault Secrets User` |

Each hosted-agent instance identity:

| Scope | Role |
|---|---|
| Foundry account | `Cognitive Services User` |
| Storage account | `Storage Blob Data Contributor` |

> A hosted agent authenticates as its per-agent `instance_identity`. If artifact upload
> fails with `AuthorizationFailure`, first confirm storage is network-reachable (§8), then
> confirm the identity actually used: log the storage-token `oid` in the container and grant
> that principal `Storage Blob Data Contributor`.

## 6. Redeploy the API and portal

```powershell
# API
az acr build --registry acr<token> --image fsi-api:latest ./api
az containerapp update -n ca-api-<env> -g rg-<env> `
  --image acr<token>.azurecr.io/fsi-api:latest `
  --revision-suffix ("v" + (Get-Date -Format 'MMddHHmmss'))

# Portal (NEXT_PUBLIC_API_BASE_URL is baked at build time)
az acr build --registry acr<token> --image fsi-portal:latest `
  --build-arg NEXT_PUBLIC_API_BASE_URL=<API_URL> ./portal
az containerapp update -n ca-portal-<env> -g rg-<env> `
  --image acr<token>.azurecr.io/fsi-portal:latest `
  --revision-suffix ("v" + (Get-Date -Format 'MMddHHmmss'))
```

The API's environment variables (`PROJECT_ENDPOINT`, `STORAGE_BLOB_ENDPOINT`,
`ARTIFACTS_CONTAINER`, `AZURE_CLIENT_ID`, `APPLICATIONINSIGHTS_CONNECTION_STRING`) are wired
directly from infra outputs in `infra/main.bicep`, so a fresh image picks them up without
manual `az containerapp update --set-env-vars`.

## 7. Validation

```powershell
$env:API_BASE_URL = "<API_URL>"
python scripts/validate.py                     # all scenarios
python scripts/validate.py pe-lbo              # one scenario
```

The validator submits to `/api/run`, reads the SSE stream, downloads the produced artifact,
and asserts it is a real OOXML file (PK zip signature). Exit code is non-zero on any
failure, so it doubles as a CI gate.

Quick API checks:

```powershell
Invoke-RestMethod <API_URL>/api/health       # { "status": "ok", ... }
Invoke-RestMethod <API_URL>/api/scenarios
Invoke-RestMethod <API_URL>/api/toolboxes
```

## 8. Known gotchas (these caused real, hard-to-diagnose failures)

- **Storage MUST be network-reachable.** The hosted-agent compute and the VNet-less
  Container Apps BFF reach Blob Storage over the public endpoint, gated by Entra ID RBAC only
  (`allowSharedKeyAccess=false`, `allowBlobPublicAccess=false`). If
  `publicNetworkAccess=Disabled` with no private endpoints for both networks, artifact upload
  fails with `AuthorizationFailure "This request is not authorized..."` — the SAME message as
  missing RBAC, so it is easy to misdiagnose as an identity problem (all three agent instance
  identities already hold `Storage Blob Data Contributor`). `infra/modules/storage.bicep` keeps
  `publicNetworkAccess=Enabled`, but a subscription **Azure Policy** with a `modify`/remediation
  effect can flip it back to `Disabled` *after* deploy — often minutes to hours later — silently
  breaking every artifact download. `deploy.ps1` re-asserts `Enabled` at step 1 and again at
  **step 7b** (after agents, before validation); if it regresses again later, repair it with:
  `az storage account update -n <acct> -g <rg> --public-network-access Enabled --default-action Allow --bypass AzureServices`.
  For a durable fix, request a **policy exemption** for the resource group, or add private
  endpoints for both the agent compute and the Container Apps environment. RBAC alone is not
  sufficient.
- **Dead `sandbox:/mnt/data` links are stripped by the BFF.** The model often narrates
  `[Download the workbook](sandbox:/mnt/data/...)` links that only resolve inside the code
  interpreter sandbox and dead-end at the portal origin. The real download is the artifact
  **button** the BFF renders from the blob sentinel, so `orchestrator._strip_sandbox_links`
  removes those links from the narrative before it reaches the portal.
- **Sync the runtime into `_azd/agent-src` before `azd deploy`** (verify with `Get-FileHash`).
- **Two toolbox connections, one native tool.** The runtime opens the scenario toolbox
  **twice**: (1) a `load_tools=False` *skills* connection consumed via `as_skills_provider()`
  and entered with `async with skills_toolbox:` in `main()` (so `load_skill` works); and (2) a
  `load_tools=True` *tools* connection placed in the agent's `tools` list with
  `allowed_tools = {web} ∪ {sec_edgar___*}`, which routes `web_search` + SEC EDGAR **through
  the governed toolbox**. `code_interpreter` is deliberately EXCLUDED from that allow-list and
  executed as the Foundry-native hosted tool — the preview toolbox `code_interpreter` returns
  a reproducible server-side 500, and excluding it also stops the broken toolbox CI from
  shadowing the working native one. Do NOT add `code_interpreter` to the allow-list.
- **Invoke hosted agents with Responses background mode + poll**, not plain `stream:false`.
  Background requires `store:true`. The final text carries the `<<<ARTIFACT ...>>>` sentinel.
- **The host strips Code Interpreter content types** from the outer `/responses` output, so
  artifacts can only be detected via the egress-middleware sentinel, never by scanning the
  outer response.
- **Framework instrumentation is intentionally disabled** in the runtime (a core-1.10.0
  span-attr serialization bug crashes on `AutoCodeInterpreterToolParam`).
- **`az` credential lookups flake** (`AzureCLICredential: exit status 1`) under back-to-back
  deploys. Deploy one service at a time and retry — `deploy.ps1` retries up to 3×.
- **`az acr build` crashes cosmetically on Windows** with `UnicodeEncodeError: 'charmap'
  codec can't encode ...` while streaming the server-side build logs. `az.cmd` launches
  `python.exe -I` (isolated mode), so `PYTHONUTF8`/`PYTHONIOENCODING` are ignored and (because
  the output is piped) `chcp 65001` cannot help either. The build itself SUCCEEDS server-side —
  only the log stream crashes, and the non-zero exit makes it look like a build failure.
  `deploy.ps1` avoids this entirely: `Invoke-AcrBuild` captures the queued run id (printed before
  the crash), ignores the cosmetic streaming failure, and polls `az acr task show-run` for the
  authoritative status. If you build manually, verify with
  `az acr task list-runs --registry <acr> --top 1 -o table` instead of trusting the exit code.
- **SEC EDGAR (optional):** requires `SEC_EDGAR_USER_AGENT` (a real contact string) on the MCP
  Container App; the Foundry gateway injects the shared-secret header (`x-fsi-mcp-key`).
  Toggle by setting/clearing `SEC_EDGAR_MCP_URL`. Upstream `sec-edgar-mcp` is AGPL-3.0 —
  review licensing before commercial redistribution.
- **Transient model errors:** heavy single prompts (deep SEC retrieval + full multi-sheet
  DCF) can 408-timeout at the model layer (~360s). The BFF mitigates with one retry, one
  corrective artifact turn, and finally a **type-correct** fallback so the portal always has a
  downloadable file whose type matches the scenario — a `.pptx` deck for `ib-pitch`, a `.xlsx`
  workbook for the equity/LBO scenarios (built dependency-free in `orchestrator.py`). That
  fallback is demo resilience, not the primary path.
- **Transient 500 on the poll GET:** the BFF submits agent runs in background mode
  (`store=true`) and polls `GET .../responses/{id}`. The Foundry gateway occasionally returns a
  cosmetic `500` (or `429`/`502`/`503`/`504`) on an otherwise-healthy in-progress run. Because the
  run is stored server-side, re-reading its status is idempotent, so `_poll_once_sync` retries
  transient 5xx/429 and network errors in place (5 attempts, linear backoff) instead of failing the
  scenario. A single un-retried `500` on the poll — not a real run failure — is what previously
  caused sporadic 1/3 or 2/3 validation results.

## 9. Extending to more live data sources

The scenarios already source real financials from **SEC EDGAR** (self-hosted MCP) plus
**web search**. To add richer vendor data:

1. Store vendor endpoints and secrets in Key Vault.
2. Register vendor MCP tools or toolbox tools in the Foundry project.
3. Expose each tool only through the scenario toolbox that needs it.
4. Update the corresponding skill instructions to use the new tool where it improves coverage.

| Workflow need | Vendor MCP examples |
|---|---|
| Public-company filings and XBRL financials | SEC EDGAR via `sec-edgar-mcp` |
| Market data, estimates, comps beyond filings | FactSet, LSEG, Morningstar |
| Private-company and sponsor data | PitchBook, Chronograph |
| Credit and issuer context | Moody's |
| Transcripts and news | Aiera, MT Newswire |
| Source documents | Box, Egnyte |

## 10. Teardown

Delete the environment's resource group:

```powershell
az group delete --name rg-<env> --yes --no-wait
```

> **Purge the soft-deleted Foundry account or you leak model quota.** Deleting the RG
> soft-deletes the `Microsoft.CognitiveServices` (Foundry) account, and its model-deployment
> TPM allocation stays counted against your regional quota until the account is *purged*.
> Leftover soft-deleted accounts are a common cause of a later deploy failing step 1 with
> `InsufficientQuota` even though no live resources remain. After the RG delete completes:
>
> ```powershell
> # list soft-deleted accounts still holding quota
> az cognitiveservices account list-deleted -o table
> # purge the one for this env (original RG name + account name from the list)
> az cognitiveservices account purge --location <location> --resource-group rg-<env> --name <aif-token>
> ```
>
> Verify the quota came back with
> `az cognitiveservices usage list --location <location> --query "[?name.value=='OpenAI.GlobalStandard.gpt-5.4']"`.

## 11. Official references

- [Microsoft Foundry hosted agents](https://learn.microsoft.com/azure/ai-foundry/agents/concepts/hosted-agents?view=foundry)
- [Foundry Agent Service runtime components](https://learn.microsoft.com/azure/ai-foundry/agents/concepts/runtime-components?view=foundry)
- [Foundry tools overview](https://learn.microsoft.com/azure/ai-foundry/agents/how-to/tools/overview?view=foundry)
- [Anthropic financial-analysis skills](https://github.com/anthropics/financial-services/tree/main/plugins/vertical-plugins/financial-analysis/skills)
- [`sec-edgar-mcp`](https://github.com/stefanoamorelli/sec-edgar-mcp)
