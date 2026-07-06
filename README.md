# FSI Multi-Agent Demo — Azure AI Foundry

A multi-agent **Financial Services** demo on **Azure AI Foundry Agent Service**,
adapting Anthropic's [`financial-analysis`](https://github.com/anthropics/financial-services)
plugin skills onto Foundry prompt agents + toolboxes, fronted by a branded web portal
on Azure Container Apps.

**Three FSI scenarios:**

| Scenario | Agents | Deliverable |
|----------|--------|-------------|
| Equity Research & Valuation | 3-Statement → DCF → Comps | `.xlsx` valuation package |
| Investment Banking Pitch | Competitive-Analysis → PPTX-Author → Deck-QC | `.pptx` pitch deck |
| Private Equity LBO Screening | LBO → Model-Audit | `.xlsx` LBO model |

All figures are **synthetic** (fictional *NovaGrid Technologies* + peers). Not
investment advice.

## Portal

https://ca-portal-fsi-demo.orangecoast-891e69ba.eastus2.azurecontainerapps.io

## Layout

```
infra/     Bicep IaC (Foundry, Container Apps, ACR, Storage, Key Vault, App Insights)
agents/    Skill→agent conversion, toolbox + agent creation scripts, manifest
api/       FastAPI orchestrator-worker backend (SSE streaming, artifact download, OTel)
portal/    Next.js branded portal (scenario picker, live stream, downloads)
data/      Synthetic datasets (NovaGrid + peers)
scripts/   Scenario eval harness
docs/      Runbook
```

## Full docs

See **[docs/runbook.md](docs/runbook.md)** for endpoints, architecture, demo script,
observability, operations (rebuild/redeploy), enabling real vendor MCP data sources,
and teardown.
