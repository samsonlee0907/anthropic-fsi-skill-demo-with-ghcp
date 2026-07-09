"""Scenario / agent configuration for the hosted-agent BFF.

The BFF invokes the deployed Foundry hosted agents (one per scenario) over their
Responses endpoints in background mode; each agent loads its Anthropic skills over
toolbox MCP, builds artifacts with native code_interpreter, and its
ArtifactEgressMiddleware uploads them to the private `artifacts` blob container,
appending a `<<<ARTIFACT name=<f> blob=<container>/<path>>>>` sentinel to the
response text.

All resource-specific values come from the environment (set by the Container App
from the infra outputs). No resource names are hardcoded, so the same image
deploys unchanged against any environment.
"""
import os


def _require_env(name: str, hint: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(
            f"Required environment variable {name} is not set. {hint}"
        )
    return val


# Foundry project endpoint the BFF calls (form:
# https://<account>.services.ai.azure.com/api/projects/<project>).
PROJECT_ENDPOINT = _require_env(
    "PROJECT_ENDPOINT",
    "Set it to the AZURE_AI_PROJECT_ENDPOINT infra output.",
).rstrip("/")

# Private blob storage the hosted agents egress artifacts to; the BFF streams them back.
STORAGE_BLOB_ENDPOINT = _require_env(
    "STORAGE_BLOB_ENDPOINT",
    "Set it to the AZURE_STORAGE_BLOB_ENDPOINT infra output "
    "(form: https://<account>.blob.core.windows.net).",
).rstrip("/")
ARTIFACTS_CONTAINER = os.environ.get("ARTIFACTS_CONTAINER", "artifacts")

# Scenario key -> deployed hosted-agent name. Names are unversioned so the same
# config serves every environment (each environment is its own Foundry project).
AGENT_NAMES = {
    "equity-research": os.environ.get("AGENT_EQUITY", "fsi-equity"),
    "ib-pitch": os.environ.get("AGENT_IB", "fsi-ib-pitch"),
    "pe-lbo": os.environ.get("AGENT_LBO", "fsi-pe-lbo"),
}


def agent_responses_base(scenario_key: str) -> str:
    """Base URL for a deployed agent's OpenAI-Responses protocol endpoint."""
    name = AGENT_NAMES[scenario_key]
    return f"{PROJECT_ENDPOINT}/agents/{name}/endpoint/protocols/openai/responses"


DISCLAIMER = (
    "All figures are AI-generated from public SEC filings and web sources for "
    "demonstration only. Not investment advice."
)

# Each scenario is served by ONE deployed Microsoft Agent Framework HOSTED agent that
# natively loads its bound Anthropic skills (load_skill progressive disclosure over the
# scenario toolbox MCP) and uses Foundry-native code_interpreter/web_search plus SEC
# EDGAR public-filing tools backed by the open-source sec-edgar-mcp package. Every
# scenario analyses a REAL public company: the agent sources its numbers live from SEC
# EDGAR filings and web search (no bundled dataset), then models them in code_interpreter.
SCENARIOS = {
    "equity-research": {
        "title": "Equity Research & Valuation",
        "tagline": "SEC-filing-grounded DCF + comparables + integrated 3-statement model.",
        "toolbox": "tb-equity-research",
        "brief": (
            "Build DCF, trading-comparables, and integrated 3-statement valuation models "
            "for a real public company, grounded in its latest SEC EDGAR filings and live "
            "web context, and deliver an institutional-quality Excel valuation package."
        ),
        "skills": [
            "dcf-model", "comps-analysis", "3-statement-model",
            "xlsx-author", "clean-data-xls", "audit-xls",
        ],
    },
    "ib-pitch": {
        "title": "Investment Banking Pitch",
        "tagline": "Competitive landscape, pitch deck authoring, and deck QC.",
        "toolbox": "tb-ib-pitch",
        "brief": (
            "Build the competitive landscape for a real public company, author a "
            "client-ready pitch deck (.pptx) with supporting comps exhibits grounded in "
            "SEC EDGAR filings and web context, and run a deck QC pass before delivery."
        ),
        "skills": [
            "competitive-analysis", "comps-analysis", "pptx-author",
            "ppt-template-creator", "deck-refresh", "ib-check-deck", "xlsx-author",
        ],
    },
    "pe-lbo": {
        "title": "Private Equity LBO Screening",
        "tagline": "LBO model build and model-integrity audit.",
        "toolbox": "tb-pe-lbo",
        "brief": (
            "Screen a real public company as an LBO candidate: pull its financials from "
            "SEC EDGAR, build the LBO model (sources & uses, debt schedule, returns), "
            "then audit the model's integrity."
        ),
        "skills": ["lbo-model", "xlsx-author", "clean-data-xls", "audit-xls"],
    },
}

# Default one-click prompts. Each targets a REAL public company (Microsoft / MSFT) and
# is answered entirely from SEC EDGAR filings + web search + code_interpreter -- there is
# no bundled synthetic dataset. Users can edit the mandate to analyse any public ticker.
DEFAULT_PROMPTS = {
    "equity-research": "Produce an equity research valuation package for Microsoft Corporation (ticker MSFT). Use SEC EDGAR to pull Microsoft's latest 10-K and 10-Q filing metadata and key XBRL financials (revenue, operating income, net income, shares outstanding), cite the filing URLs and dates, use web search for the current share price and market context, then build a base/bull/bear DCF, trading comps versus large-cap software peers, and a triangulated valuation range.",
    "ib-pitch": "Prepare a concise IB pitch for Microsoft Corporation (ticker MSFT): competitive positioning versus large-cap software and cloud peers, a short client-ready pitch deck with a competitive-positioning slide, and a QC pass on the deck. Use SEC EDGAR for Microsoft's latest 10-K headline financials and cite the filing URLs; use web search for current market context.",
    "pe-lbo": "Screen Microsoft Corporation (ticker MSFT) as an illustrative LBO candidate. Use SEC EDGAR to pull Microsoft's latest 10-K financials (revenue, operating income, total debt, cash), cite the filing URLs and dates, then build the LBO model (sources & uses, debt schedule, returns), estimate IRR/MOIC, and audit the model.",
}

# A second one-click preset that analyses a DIFFERENT real public company, so the demo
# shows the same governed pattern generalising across tickers. Surfaced in the portal as
# a "Try another company" button next to the Microsoft default.
EDGAR_PROMPTS = {
    "equity-research": "Produce an equity research valuation package for NVIDIA Corporation (ticker NVDA). Use SEC EDGAR to pull NVIDIA's latest 10-K/10-Q filing metadata and key XBRL metrics (revenue, operating income, net income), cite the filing URLs and dates, add web-sourced market context and share price, then build a base/bull/bear DCF and trading comps versus semiconductor peers.",
    "ib-pitch": "Prepare an IB pitch for Apple Inc. (ticker AAPL): competitive positioning versus hardware and services peers, a short pitch deck with a competitive-positioning slide, and a deck QC pass. Use SEC EDGAR for Apple's most recent 10-K headline financials, cite the filing URL and date, and use web search for market context.",
    "pe-lbo": "Screen Tesla, Inc. (ticker TSLA) as an illustrative LBO candidate. Use SEC EDGAR to pull Tesla's latest 10-K financials (revenue, operating income, total debt), cite the filing URL and date, then build the LBO model and estimate IRR/MOIC.",
}


# Static metadata to enrich the live toolbox listing (the list API returns only
# name/id/version, not the per-version description/tools). Keys match toolbox names.
TOOLBOX_META = {
    "tb-equity-research": {
        "description": "Equity research & valuation tools: build DCF / comps / 3-statement models and ground with SEC EDGAR filings plus live web context.",
        "tools": ["code_interpreter", "web_search", "sec_edgar_mcp"],
    },
    "tb-ib-pitch": {
        "description": "IB pitch tools: competitive analysis, PPTX deck authoring, deck QC, and SEC EDGAR public-company filing support.",
        "tools": ["code_interpreter", "web_search", "sec_edgar_mcp"],
    },
    "tb-pe-lbo": {
        "description": "PE LBO tools: build LBO models, audit model integrity, and use SEC EDGAR for public-target financials where applicable.",
        "tools": ["code_interpreter", "web_search", "sec_edgar_mcp"],
    },
}
