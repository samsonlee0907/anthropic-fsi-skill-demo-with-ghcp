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
from pathlib import Path


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
    "All figures are ILLUSTRATIVE and SYNTHETIC for demo purposes only "
    "(fictional company NovaGrid Technologies and fictional peers). Not investment advice."
)

# Each scenario is served by ONE deployed Microsoft Agent Framework HOSTED agent that
# natively loads its bound Anthropic skills (load_skill progressive disclosure over the
# scenario toolbox MCP) and uses Foundry-native code_interpreter/web_search plus SEC
# EDGAR public-filing tools backed by the open-source sec-edgar-mcp package.
SCENARIOS = {
    "equity-research": {
        "title": "Equity Research & Valuation",
        "tagline": "DCF + comparables + integrated 3-statement model for NovaGrid Technologies.",
        "toolbox": "tb-equity-research",
        "brief": (
            "Build DCF, trading-comparables, and integrated 3-statement valuation models "
            "with optional SEC EDGAR public-filing grounding, and deliver an "
            "institutional-quality Excel valuation package."
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
            "Build the competitive landscape, author a client-ready pitch deck (.pptx) with "
            "supporting comps exhibits, optionally ground public-company claims in SEC EDGAR, "
            "and run a deck QC pass before delivery."
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
            "Screen the target as an LBO candidate: build the LBO model (sources & uses, "
            "debt schedule, returns), optionally ground public-target financials in SEC EDGAR, "
            "then audit the model's integrity."
        ),
        "skills": ["lbo-model", "xlsx-author", "clean-data-xls", "audit-xls"],
    },
}

DEFAULT_PROMPTS = {
    "equity-research": "Produce an equity research valuation package for NovaGrid Technologies using the synthetic dataset. If a real public ticker is provided, use SEC EDGAR for filing-backed financials and cite the filing URLs. Include a base/bull/bear DCF, trading comps vs the peer set, and a triangulated valuation range.",
    "ib-pitch": "Prepare a concise IB pitch for NovaGrid Technologies: competitive positioning vs peers, a short pitch deck, and a QC pass on the deck. If a real public ticker is provided, use SEC EDGAR for 10-K/10-Q context and cite filing URLs.",
    "pe-lbo": "Screen NovaGrid Technologies as an LBO candidate using the synthetic assumptions. If a real public ticker is provided, use SEC EDGAR for filing-backed financials and cite filing URLs. Build the LBO model, estimate returns (IRR/MOIC), and audit the model.",
}


def _data_dir() -> Path:
    # Single source of truth: api/data (also what the Docker image ships as /app/data).
    return Path(os.environ.get("DATA_DIR", str(Path(__file__).resolve().parents[1] / "data")))


def load_data_context() -> str:
    """Concatenate the synthetic datasets into a compact context block injected into agents.

    code_interpreter has no internet, so the data is supplied in-context.
    """
    d = _data_dir()
    if not d.exists():
        return "(No synthetic dataset directory found.)"
    parts = []
    wanted = [
        "companies.json",
        "novagrid_financials.csv",
        "novagrid_assumptions.json",
        "peer_comps.csv",
        "lbo_assumptions.json",
        "market_context.json",
    ]
    for name in wanted:
        p = d / name
        if p.exists():
            try:
                parts.append(f"### FILE: {name}\n{p.read_text(encoding='utf-8')}")
            except Exception:  # noqa: BLE001
                pass
    return "\n\n".join(parts) if parts else "(No synthetic dataset files found.)"


DATA_CONTEXT = load_data_context()

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
