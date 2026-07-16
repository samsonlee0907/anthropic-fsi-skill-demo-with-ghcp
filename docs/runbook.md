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
  `agentModelDeploymentName` in the list. For a quick capacity override without editing bicep,
  pass `deploy.ps1 -ModelCapacity <thousands-of-TPM>` (default `150`). Note: `gpt-5.4`
  GlobalStandard quota is **subscription-global** — every region reports the same used/limit — so
  freeing it means deleting *and purging* an unused Foundry (AIServices) account, not just
  switching region.
- Region must satisfy **both** model quota **and** Container Apps capacity; these are independent.
  East US 2 is the tested default. Some regions (observed: Sweden Central) can have model quota but
  be out of Container Apps managed-environment capacity (`ManagedEnvironmentCapacityHeavyUsageError`)
  — switch regions if you hit that at the infra step.
- `az`, `azd` (with the Foundry extensions — install the GA unified bundle with
  `azd ext install microsoft.foundry`, or the legacy individual betas
  `azd extension install azure.ai.agents azure.ai.skills azure.ai.connections azure.ai.toolboxes`;
  either works, verify with `azd ai agent --help`),
  `gh` (authenticated), Python 3.11+ (for the API/portal builds and `scripts/validate.py`).
- `az login`. Skills, the SEC connection and the toolboxes are provisioned declaratively with
  `azd ai` (`scripts/provision_foundry.ps1`) — no Python provisioning dependencies. Multi-subscription
  users: `az account set --subscription <id>` or pass `-SubscriptionId <id>` to `deploy.ps1`.
- `deploy.ps1` resolves your object ID through a fallback chain: `-PrincipalId` if you pass it,
  then `az ad signed-in-user show`, then the `oid` claim decoded from the ARM access token.
  The last step covers **Conditional Access / CAE** tenants where a token challenge
  (`TokenCreatedWithOutdatedPolicies`) makes the Microsoft Graph call return empty — that used to
  pass a blank `developerPrincipalId`, silently skip the developer RBAC assignment, and fail skill
  registration with an `agents/read` permission error. If all three fail the script now **stops
  with a clear message** telling you to pass `-PrincipalId <objectId>` instead of deploying half
  a stack.
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
2. **(Optional) Deploy the SEC EDGAR MCP** Container App and generate its shared secret:
   ```powershell
   ./scripts/deploy_sec_edgar.ps1 -ResourceGroup rg-<env> -RegistryName acr<token> `
     -UserAssignedIdentityId <AZURE_MANAGED_IDENTITY_ID> `
     -SecEdgarUserAgent "Your Name (you@example.com)" -FsiMcpKey <generated-secret>
   ```
3. **Map infra outputs into the azd agent environment** (seeds `FOUNDRY_PROJECT_ENDPOINT`, the
   derived `TOOLBOX_ENDPOINT_*`, `SEC_EDGAR_MCP_URL` / `FSI_MCP_KEY`):
   ```powershell
   ./scripts/set_azd_env_from_infra.ps1 -ProjectEndpoint <...> -StorageBlobEndpoint <...> `
     -ModelDeploymentName gpt-5.4 -EnvName <env>
   ```
4. **Provision skills + SEC connection + toolboxes declaratively** with `azd ai` (GA):
   ```powershell
   # Reads the azd env seeded in step 3 (FOUNDRY_PROJECT_ENDPOINT + SEC_EDGAR_MCP_URL/FSI_MCP_KEY):
   ./scripts/provision_foundry.ps1 -EnvName <env> -AzdDir agents/hosted/_azd
   ```
   This runs `azd ai skill create` for each of the 12 skills (content fetched from the pinned
   Anthropic commit `-SkillsRef`, then overlaid with any repo-local files in
   `skills/overrides/`), `azd ai connection create sec-edgar --kind remote-tool
   --auth-type custom-keys` (only when `SEC_EDGAR_MCP_URL` is set), and
   `azd ai toolbox create --from-file <toolbox>.json` + `azd ai toolbox publish` for each of the 3
   scenario toolboxes (publish promotes the default version — the MCP endpoint serves the default).
5. **Deploy the 3 hosted agents** (see §4).
6. **Grant agent instance-identity RBAC** (see §5).
7. **Build + deploy API and portal** images (see §6).
8. **Validate** all three scenarios (see §7).

## 4. Deploy / redeploy the hosted agents

The hosted-agent module (`agents/hosted/*.py`) is the source of truth; the deployed copy is
`agents/hosted/_azd/agent-src/*.py`. Always sync before deploying — a stale copy silently
ships old behavior.

```powershell
Copy-Item agents/hosted/fsi_hosted_agent.py agents/hosted/_azd/agent-src/ -Force
Copy-Item agents/hosted/fsi_artifact_egress.py agents/hosted/_azd/agent-src/ -Force
Copy-Item agents/hosted/requirements.txt       agents/hosted/_azd/agent-src/ -Force
Get-FileHash agents/hosted/fsi_hosted_agent.py, agents/hosted/_azd/agent-src/fsi_hosted_agent.py

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
and asserts it is a real OOXML file (PK zip signature) **and not the API fallback summary
artifact**. Exit code is non-zero on any failure, so it doubles as a CI gate.

For a UI-path validation that also refreshes the README screenshots, run:

```powershell
./scripts/capture_screenshots.ps1 -PortalUrl <PORTAL_URL>
```

That harness drives the live portal headlessly, waits for the artifact buttons, downloads
the generated files, and now fails if a scenario only emits a fallback `*_agent_summary.*`
artifact or misses an expected default file type.

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
  `publicNetworkAccess=Enabled`, but a subscription/management-group **Azure Policy** with a
  `modify` effect can flip it back to `Disabled` — at create time and again *after* deploy,
  minutes to hours later — silently breaking every artifact download. **A `modify` policy is the
  nasty case:** `az storage account update --public-network-access Enabled` returns success but
  the value stays `Disabled` (the policy re-applies on every write), so a guard that only checks
  the exit code silently "passes". `deploy.ps1` calls `scripts/ensure_storage_public.ps1` at
  step 1 and again at **step 7b**; that helper re-reads the actual value and, if a policy is
  reverting it, **self-heals** by creating a resource-group-scoped **Waiver policy exemption** for
  the offending assignment, then re-applies and re-verifies. See *Storage public network access &
  policy exemptions* at the end of this section if you can't create exemptions.
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
  `allowed_tools = {tool_search, call_tool}` — the GA Tool Search meta-tools, through which
  `web` and the `sec-edgar___*` tool set are discovered and executed **through the governed
  toolbox** (each toolbox declares `toolbox_search_preview`). This sample deliberately keeps
  `code_interpreter` OUT of that allow-list and runs it as the Foundry-native hosted tool:
  the portal's artifact egress is implemented against the native sandbox `container_id`, and
  the live UI / validator path is verified against that native flow. Foundry's GA toolbox
  docs include Code Interpreter support, but this sample's validated download path is the
  native one. Do NOT add `code_interpreter` to the allow-list unless you also rework and
  re-validate artifact delivery.
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

### Storage public network access & policy exemptions

If your subscription enforces a `modify`/`deny` policy on storage `publicNetworkAccess`, you need
either a policy exemption or private endpoints. `scripts/ensure_storage_public.ps1` automates the
exemption path (it needs `Microsoft.Authorization/policyExemptions/write` — Owner or Resource
Policy Contributor on the resource group). To do it by hand, discover the governing assignment and
create a Waiver:

```powershell
# 1. Find the assignment(s) effective on the storage account
az policy assignment list --disable-scope-strict-match --scope <storage-account-id> -o table

# 2. Create an RG-scoped Waiver exemption (for an initiative, add the definition ref id)
az policy exemption create --name fsi-storage-public-network-waiver `
  --resource-group <rg> --policy-assignment <assignment-id> `
  --exemption-category Waiver `
  --policy-definition-reference-ids <StoragePublicNetworkModify-ref-id>

# 3. Re-apply and confirm it holds (should print Enabled)
az storage account update -n <acct> -g <rg> --public-network-access Enabled --default-action Allow --bypass AzureServices
az storage account show -n <acct> -g <rg> --query publicNetworkAccess -o tsv
```

If you cannot create exemptions (no policy permissions), ask a Policy owner for an exemption or
exclusion for the resource group, or adopt the private-endpoint architecture below. RBAC alone is
not sufficient — the request never reaches the RBAC check when the network path is closed.

Before a live demo, rerun `scripts/ensure_storage_public.ps1` or at least verify
`az storage account show -n <acct> -g <rg> --query publicNetworkAccess -o tsv` still returns
`Enabled`. A governed subscription can re-disable it days after a successful deploy.

### Private networking (enterprise alternative)

To satisfy a "no public storage" policy natively instead of exempting it, keep
`publicNetworkAccess=Disabled` and give **both** consumers a private path to Blob Storage:

- a **Blob private endpoint** into your VNet plus the `privatelink.blob.core.windows.net`
  private DNS zone linked to that VNet;
- a **VNet-integrated Container Apps environment** for the BFF (the `infrastructureSubnetId` must
  be set at environment *create* time — it can't be toggled on an existing environment, so this is
  a create-time redesign of `infra/modules/*`);
- **Foundry account network injection** so the managed hosted-agent compute resolves storage over
  the private endpoint (a separate Foundry networking-enabled setup — the default hosted-agent
  compute is Microsoft-managed and not joined to your VNet).

This is the correct long-term posture for regulated environments but requires the create-time
infra changes above; the shipped template uses the public-endpoint + RBAC path, which is why the
storage guard exists.

## 9. Extending to more live data sources

> **SEC EDGAR is just one of many MCP servers you can connect — it is not special.** It is simply
> the reference `remote-tool` connection wired into this demo. A toolbox can host any number of
> governed MCP connections, and **each additional MCP server you attach broadens the agents' reach
> and makes them measurably more capable.** The same
> `azd ai connection create <name> --kind remote-tool --auth-type custom-keys` pattern applies to
> every one; GA Tool Search then surfaces the new tools automatically, with no hosted-agent code
> change. The table below lists FSI data sources, but the pattern is not limited to them — any MCP
> server (databases, storage, internal APIs, other SaaS tools) can be added the same way.

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

### GA toolbox alignment

Foundry **toolbox is GA** (see the [toolbox how-to](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/toolbox?pivots=python)).
This asset is provisioned and consumed through the GA contract: the MCP **consumer** endpoint
(`{project_endpoint}/toolboxes/{name}/mcp?api-version=v1`, always serving the promoted default
version), the `https://ai.azure.com/.default` auth scope, and `{server_label}___{tool_name}` MCP
tool naming. The following GA capabilities are already adopted (validate any change with
`scripts/validate.py`, which exercises all three scenarios):

1. **Tool Search is enabled on every toolbox.** Each toolbox declares
   `{ "type": "toolbox_search_preview", "name": "tool_search" }`, so its `tools/list` returns only
   the `tool_search` + `call_tool` meta-tools; `web` and `sec-edgar___*` (the full ~15-tool SEC set)
   are discovered via `tool_search` and executed via `call_tool`, keeping model context flat as a
   toolbox grows. The runtime's allow-list is therefore `{tool_search, call_tool}` and the agent
   instructions tell the model to search for a capability before using it. See
   [Tool Search](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/tool-search).
2. **Provisioning is declarative** via `scripts/provision_foundry.ps1`: `azd ai skill create`,
   `azd ai connection create sec-edgar --kind remote-tool --auth-type custom-keys` (preserves our
   self-hosted SEC EDGAR MCP + shared-secret header auth as a governed project connection), and
   `azd ai toolbox create --from-file` + `azd ai toolbox publish`. The generated toolbox spec
   references `connections`, `tools`, and `skills` by name with no embedded credentials. This
   retired the REST-based provisioning scripts and the preview-era
   `Foundry-Features: Toolboxes=V1Preview` header workaround (no longer required on GA).
3. **This sample keeps `code_interpreter` native, not in the toolbox.** Foundry's GA toolbox docs
   include Code Interpreter support, but this repo's validated artifact-egress path still runs CI
   via `FoundryChatClient`: `ArtifactEgressMiddleware` depends on the native sandbox
   `container_id`, and the live portal / validator / screenshot flow is verified against that
   native path. The toolbox remains the governed surface for skills plus SEC EDGAR / web search.
4. **No `FOUNDRY_`-prefixed custom container env vars.** The platform reserves the `FOUNDRY_`
   prefix and may overwrite such vars at runtime, so the container runtime reads
   `FSI_PROJECT_ENDPOINT` (mapped from the azd-side `FOUNDRY_PROJECT_ENDPOINT` in `azure.yaml`).
   Keep your own endpoint/config vars unprefixed.

> **RBAC (GA):** the GA toolbox prerequisites ask for the **Foundry User** role on the Foundry
> *project* for every identity that touches a toolbox (developer, hosted-agent identity, and —
> for OAuth tools — the end user). This demo's account-scoped `Cognitive Services User` grants
> satisfy the current runtime calls; add **Foundry User** on the project if you extend the
> toolbox with OAuth-based or project-scoped tools.

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

- [Microsoft Foundry hosted agents](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents)
- [Foundry Agent Service runtime components](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/runtime-components)
- [Foundry tools overview](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/tool-catalog)
- [Anthropic financial-analysis skills](https://github.com/anthropics/financial-services/tree/main/plugins/vertical-plugins/financial-analysis/skills)
- [`sec-edgar-mcp`](https://github.com/stefanoamorelli/sec-edgar-mcp)
