"""Scenario / agent-pipeline configuration and synthetic-data loading."""
import os
from pathlib import Path

PROJECT_ENDPOINT = os.environ.get(
    "PROJECT_ENDPOINT",
    "https://aif66lhnuec.services.ai.azure.com/api/projects/proj-fsi-demo",
).rstrip("/")

# Default deployment used by the orchestrator synthesis step.
SYNTH_MODEL = os.environ.get("SYNTH_MODEL", "gpt-5.1")

DISCLAIMER = (
    "All figures are ILLUSTRATIVE and SYNTHETIC for demo purposes only "
    "(fictional company NovaGrid Technologies and fictional peers). Not investment advice."
)

# Each scenario runs its specialist agents in order, then the orchestrator synthesizes.
# Agent names must match agents/definitions/agents-manifest.json (created by create_agents.py).
SCENARIOS = {
    "equity-research": {
        "title": "Equity Research & Valuation",
        "tagline": "DCF + comparables + integrated 3-statement model for NovaGrid Technologies.",
        "toolbox": "tb-equity-research",
        "orchestrator": "fsi-orchestrator-equity-research",
        "steps": [
            {"agent": "fsi-three-statement-agent", "label": "Building integrated 3-statement model"},
            {"agent": "fsi-dcf-agent", "label": "Running DCF valuation"},
            {"agent": "fsi-comps-agent", "label": "Building trading comparables"},
        ],
    },
    "ib-pitch": {
        "title": "Investment Banking Pitch",
        "tagline": "Competitive landscape, pitch deck authoring, and deck QC.",
        "toolbox": "tb-ib-pitch",
        "orchestrator": "fsi-orchestrator-ib-pitch",
        "steps": [
            {"agent": "fsi-competitive-analysis-agent", "label": "Analyzing competitive landscape"},
            {"agent": "fsi-pptx-author-agent", "label": "Authoring pitch deck (.pptx)"},
            {"agent": "fsi-deck-qc-agent", "label": "Running deck quality control"},
        ],
    },
    "pe-lbo": {
        "title": "Private Equity LBO Screening",
        "tagline": "LBO model build and model-integrity audit.",
        "toolbox": "tb-pe-lbo",
        "orchestrator": "fsi-orchestrator-pe-lbo",
        "steps": [
            {"agent": "fsi-lbo-agent", "label": "Building LBO model"},
            {"agent": "fsi-model-audit-agent", "label": "Auditing model integrity"},
        ],
    },
}

DEFAULT_PROMPTS = {
    "equity-research": "Produce an equity research valuation package for NovaGrid Technologies using the synthetic dataset. Include a base/bull/bear DCF, trading comps vs the peer set, and a triangulated valuation range.",
    "ib-pitch": "Prepare a concise IB pitch for NovaGrid Technologies: competitive positioning vs peers, a short pitch deck, and a QC pass on the deck.",
    "pe-lbo": "Screen NovaGrid Technologies as an LBO candidate using the synthetic assumptions. Build the LBO model, estimate returns (IRR/MOIC), and audit the model.",
}


def _data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", str(Path(__file__).resolve().parents[2] / "data" / "synthetic")))


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
        "description": "Equity research & valuation tools: build DCF / comps / 3-statement models and ground with live web/SEC data.",
        "tools": ["code_interpreter", "web_search"],
    },
    "tb-ib-pitch": {
        "description": "IB pitch tools: competitive analysis, PPTX deck authoring, and deck QC with live grounding.",
        "tools": ["code_interpreter", "web_search"],
    },
    "tb-pe-lbo": {
        "description": "PE LBO tools: build LBO models and audit model integrity with live grounding.",
        "tools": ["code_interpreter", "web_search"],
    },
}
