# Changelog

All notable changes to this solution template are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [v1.0.0] - Unreleased

First tagged release: a replicate-ready template for three scenario-based FSI hosted agents on
Microsoft Foundry (Equity Research `.xlsx`, IB Pitch `.pptx`, PE LBO `.xlsx`), each with its own
governed skills toolbox, behind a FastAPI BFF and a Next.js portal on Azure Container Apps.
Full notes: [docs/release-notes/v1.0.0.md](docs/release-notes/v1.0.0.md).

### Breaking changes

- **The model is now user-supplied.** `infra/main.bicep` requires `modelName`, `modelVersion`
  and `modelCapacity` (`modelSku` defaults to `GlobalStandard`; `agentModelDeploymentName`
  defaults to `modelName`), and `deploy.ps1` requires `-ModelName`, `-ModelVersion` and
  `-ModelCapacity`. There is no hardcoded model default. Suggested: `gpt-6-astra` `2026-09-03`
  GlobalStandard.

### Added

- Model parameters flow into the single Foundry model deployment, the hosted agents
  (`AZURE_AI_MODEL_DEPLOYMENT_NAME`) and the API; `/api/health` reports `environment_name` and
  `model_deployment_name`, and the portal shows them.
- Optional WebIQ news enrichment for `/api/run`: the portal holds the key in tab memory only and
  searches only when the user enters an explicit news query; results are passed to the agent as
  untrusted, cited context. SSE `enrichment` and `warning` events; the `X-WebIQ-Key` header is
  redacted from telemetry and validation errors.
- `get_financial_fact_pack` SEC EDGAR MCP tool: normalized annual 10-K facts with accession,
  period and unit provenance, used by all three scenario prompts.
- `-DemoStoragePolicyOptOut` / `demoStoragePolicyOptOut`: tags only the storage account with the
  documented `SecurityControl=Ignore` exclusion when a policy owner approves it. Data access stays
  Entra-only (no anonymous blobs, no shared keys).
- `scripts/validate.py --portal-origin` checks CORS preflights from the portal origin.
- Tests: API run flow and WebIQ, SEC EDGAR fact pack, validator CORS, deploy-script mapping and
  storage opt-out, portal unit tests and mocked-API Playwright browser tests; GitHub Actions CI.

### Changed

- `/api/run` reports a `complete` / `partial` / `error` outcome; a narrative-only fallback file is
  labelled `summary` and makes the run partial rather than reporting success.
- Artifact downloads return `503` (retryable) when storage is unreachable, instead of `404`.
- Agent instructions: updated disclaimer, SEC fact-pack guidance, untrusted-context handling and
  honesty wording (no claimed QC pass or approval).
- `deploy.ps1`: detects the `microsoft.foundry` azd extension, maps tenant and resource group
  into the azd env, checks every native command exit code, syncs the hosted runtime into
  `agent-src` by hash, and validates with the portal origin.
- `scripts/provision_foundry.ps1`: updates existing skills in place instead of force-recreating
  them, validates `-SkillsOnly` names and checks azd exit codes.
- `scripts/ensure_storage_public.ps1`: fails loudly when it cannot read the storage account and
  supports the approved storage-only opt-out (`-DemoPolicyOptOut`), verifying the result.
- Key Vault name is `kv-<first 12 chars of env>-<token>`, so environment names of 13-20
  characters no longer exceed the 24-character Key Vault limit. Names of 12 characters or fewer
  are unchanged.
- Portal dependencies updated (Next.js 15.5, React 19).
