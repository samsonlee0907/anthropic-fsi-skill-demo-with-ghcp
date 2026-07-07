"""Bind the registered Foundry skills to the 3 scenario toolboxes (v2 design).

Each scenario toolbox bundles the shared Foundry-native tools (code_interpreter,
web_search) PLUS the Anthropic skills relevant to that scenario, referenced by
name (following each skill's default version). At the MCP layer the skills are
exposed as SEP-2640 resources (`skill://<name>/SKILL.md`), which the hosted
scenario agents discover via `resources/list` and load on demand via the SDK
`load_skill` tool.

Cross-cutting skills (xlsx-author, clean-data-xls, audit-xls) are referenced from
multiple toolboxes; the single central skill is the source of truth.

SEC EDGAR is now bound as a GOVERNED TOOLBOX MCP TOOL (type "mcp"), not imported
in-process inside each hosted-agent container. The self-hosted `sec-edgar-mcp`
streamable-HTTP server (Container App `ca-secedgar-mcp-<env>`) is registered
with `server_url` + a shared-secret `headers` entry, so the toolbox governs which
SEC tools each scenario can call (`allowed_tools`) and the endpoint stays gated.
Configured via env:
  SEC_EDGAR_MCP_URL    full streamable-HTTP URL of the hosted MCP server (…/mcp)
  FSI_MCP_KEY          shared secret sent in the auth header
  FSI_MCP_KEY_HEADER   header name (default "x-fsi-mcp-key")
  SEC_EDGAR_DEEP_TOOLS "true" to also allow slower full-filing / full-statement tools
If SEC_EDGAR_MCP_URL is unset the MCP tool is simply omitted (skills/web still bind).

POSTing a new toolbox version does NOT move the default pointer, so after
creating the version we PATCH the toolbox to promote the new version to default.
The `beta.toolboxes` SDK surface does not exist in azure-ai-projects 2.2.0, so
this uses the same direct data-plane REST path as create_toolboxes.py:
  POST  {PE}/toolboxes/{name}/versions?api-version=v1   (create version w/ skills)
  PATCH {PE}/toolboxes/{name}?api-version=v1            (promote default_version)
Header: Foundry-Features: Toolboxes=V1Preview
Auth  : DefaultAzureCredential -> https://ai.azure.com/.default
"""
import json
import os
import sys
import urllib.error
import urllib.request

from azure.identity import DefaultAzureCredential

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import require_project_endpoint  # noqa: E402

PROJECT_ENDPOINT = require_project_endpoint()

# Compact, scenario-safe SEC EDGAR surface exposed through the toolbox MCP tool.
# These map to the tools the in-container function surface used to expose; the
# toolbox `allowed_tools` filter governs exactly what each agent can call.
SEC_EDGAR_COMPACT_TOOLS = [
    "get_cik_by_ticker",
    "get_company_info",
    "search_companies",
    "get_recent_filings",
    "get_key_metrics",
    "compare_periods",
]
# Slower, heavier tools gated behind SEC_EDGAR_DEEP_TOOLS=true.
SEC_EDGAR_DEEP_TOOLS = [
    "get_financials",
    "get_filing_sections",
    "get_company_facts",
]


def _sec_edgar_mcp_tool() -> dict | None:
    """Build the SEC EDGAR toolbox MCP tool spec from env, or None if not configured."""
    url = os.environ.get("SEC_EDGAR_MCP_URL", "").strip()
    if not url:
        return None
    key = os.environ.get("FSI_MCP_KEY", "").strip()
    header = os.environ.get("FSI_MCP_KEY_HEADER", "x-fsi-mcp-key").strip()
    allowed = list(SEC_EDGAR_COMPACT_TOOLS)
    if os.environ.get("SEC_EDGAR_DEEP_TOOLS", "false").strip().lower() in {"1", "true", "yes", "on"}:
        allowed += SEC_EDGAR_DEEP_TOOLS
    tool: dict = {
        "type": "mcp",
        "server_label": "sec_edgar",
        "server_url": url,
        "server_description": (
            "SEC EDGAR public filings and XBRL financials for US public companies "
            "(company metadata, recent 10-K/10-Q/8-K filings, key metrics, period "
            "comparisons). Real live data from the SEC EDGAR REST API."
        ),
        # allow-list keeps each scenario focused and blocks the heavier insider/XBRL
        # discovery tools the hosted server also exposes.
        "allowed_tools": allowed,
        # Unattended hosting: skill/tool calls must run without an approval round-trip.
        "require_approval": "never",
    }
    if key:
        tool["headers"] = {header: key}
    return tool


def _base_tools() -> list[dict]:
    """Governed-catalog tools every scenario toolbox carries.

    The toolbox is the SINGLE governed catalog per scenario, so it lists all three
    shared Foundry tools -- `code_interpreter`, `web_search`, and (when configured)
    the self-hosted SEC EDGAR MCP server -- alongside the scenario's skills. This
    gives one unified, portal-visible tool + skill inventory per scenario.

    IMPORTANT -- catalog vs. runtime execution: binding `code_interpreter` /
    `web_search` here makes them part of the governed CATALOG only. The hosted-agent
    runtime (fsi_hosted_agent_v3.py) consumes this toolbox as a SKILLS provider
    (`as_skills_provider()`, `load_tools=False`) and EXECUTES `code_interpreter` /
    `web_search` as the reliable Foundry-native hosted tools. It never invokes them
    THROUGH the toolbox MCP, so listing them here does NOT reintroduce the preview
    defects (toolbox-MCP `code_interpreter` 500 / post-`load_skill`
    RemoteProtocolError) and does NOT suppress the native hosted tools. Catalog and
    runtime stay consistent with no regression.

    SEC EDGAR IS consumed through the toolbox at runtime (as a hosted remote-MCP
    tool), so it is a real, callable catalog entry when SEC_EDGAR_MCP_URL is set.
    """
    tools: list[dict] = [
        {"type": "code_interpreter"},
        {"type": "web_search", "name": "web"},
    ]
    sec = _sec_edgar_mcp_tool()
    if sec:
        tools.append(sec)
    return tools

# scenario toolbox -> (description, [skill names])
TOOLBOXES = {
    "tb-equity-research": (
        "S1 Equity Research & Valuation: DCF / comps / 3-statement modelling with "
        "Excel authoring, data cleaning and model audit skills; live web/SEC grounding.",
        ["3-statement-model", "dcf-model", "comps-analysis",
         "xlsx-author", "clean-data-xls", "audit-xls"],
    ),
    "tb-ib-pitch": (
        "S2 Investment Banking Pitch: competitive & comps analysis, PPTX deck authoring, "
        "template creation, deck refresh and deck QC skills; live web grounding.",
        ["competitive-analysis", "comps-analysis", "pptx-author",
         "ppt-template-creator", "deck-refresh", "ib-check-deck", "xlsx-author"],
    ),
    "tb-pe-lbo": (
        "S3 Private Equity LBO Screening: LBO modelling with Excel authoring, "
        "data cleaning and model audit skills; live web grounding.",
        ["lbo-model", "xlsx-author", "clean-data-xls", "audit-xls"],
    ),
}


def _token() -> str:
    return DefaultAzureCredential().get_token("https://ai.azure.com/.default").token


def _call(url: str, token: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Foundry-Features", "Toolboxes=V1Preview")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    token = _token()
    ok = True
    base_tools = _base_tools()
    has_sec = any(t.get("type") == "mcp" and t.get("server_label") == "sec_edgar" for t in base_tools)
    tool_types = [t.get("server_label") if t.get("type") == "mcp" else t.get("type") for t in base_tools]
    print(f"[info] toolbox catalog tools: {tool_types}")
    print(f"[info] SEC EDGAR MCP tool {'BOUND' if has_sec else 'OMITTED (SEC_EDGAR_MCP_URL unset)'}")
    for name, (desc, skills) in TOOLBOXES.items():
        try:
            body = {
                "description": desc,
                "tools": base_tools,
                "skills": [{"type": "skill_reference", "name": s} for s in skills],
            }
            tv = _call(
                f"{PROJECT_ENDPOINT}/toolboxes/{name}/versions?api-version=v1",
                token, "POST", body,
            )
            new_ver = str(tv.get("version"))
            # POSTing a version does not move the default pointer; promote it.
            patched = _call(
                f"{PROJECT_ENDPOINT}/toolboxes/{name}?api-version=v1",
                token, "PATCH", {"default_version": new_ver},
            )
            dv = patched.get("default_version")
            print(f"[OK]   {name} -> v{new_ver} (default={dv}); {len(skills)} skills: {skills}")
        except urllib.error.HTTPError as e:  # noqa: PERF203
            ok = False
            print(f"[FAIL] {name} -> HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"[FAIL] {name} -> {str(e)[:300]}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
