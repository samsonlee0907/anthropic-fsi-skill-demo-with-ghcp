"""FSI scenario **hosted agent** (v3) — Microsoft Agent Framework + Foundry toolbox MCP.

This is the v3 runtime: one containerized Microsoft Agent Framework (MAF) agent per
FSI scenario, deployed **into Foundry Agent Service** as a genuine hosted agent
(``azd ai agent``) and served over the Foundry Responses protocol by
:class:`~agent_framework_foundry_hosting.ResponsesHostServer`.

How this differs from the v2 in-process module (``fsi_scenario_agent.py``, kept intact):

1. **Skills come over the toolbox MCP, for real.** Instead of downloading skill
   archives at startup, the agent embeds a :class:`FoundryToolbox` pointed at its
   scenario toolbox MCP endpoint and calls ``toolbox.as_skills_provider()``. The SDK
   discovers the toolbox's bound skills from the well-known ``skill://index.json``
   MCP resource, advertises each skill's name+description, and synthesizes a
   ``load_skill`` tool so the model pulls full skill bodies on demand (progressive
   disclosure, SEP-2640) — native skill consumption straight off the governed
   toolbox catalog.

2. **Public filing grounding is a GOVERNED, SELF-HOSTED MCP TOOL.** SEC EDGAR is no
   longer imported in-process in this container. Instead the ``sec-edgar-mcp`` server
   runs as its own Container App (``ca-secedgar-mcp-<env>``) and is consumed at
   runtime as a **Foundry-native hosted (remote) MCP tool** via
   ``FoundryChatClient.get_mcp_tool(name="sec_edgar", url=..., headers=..., allowed_tools=...,
   approval_mode="never_require")``. The Foundry Responses gateway connects to the remote
   server and injects our shared-secret header at request time, so the tool coexists
   cleanly with the native ``code_interpreter``/``web_search`` hosted tools. (Surfacing
   the same server as a *client-side* toolbox ``load_tools=True`` FunctionTool instead
   suppresses the native hosted tools — the model can no longer call code_interpreter —
   so the hosted remote-MCP path is used.) The same server is ALSO registered in each
   scenario toolbox as an ``mcp`` tool, keeping the toolbox the governed, discoverable
   catalog. Broader live web grounding uses the Foundry-native ``web_search`` tool.

3. **code_interpreter is the Foundry-native Responses sandbox tool**
   (``FoundryChatClient.get_code_interpreter_tool``), NOT the toolbox's. The
   toolbox-MCP ``code_interpreter`` tool returns a reproducible server-side ``500``
   in preview (confirmed on both v2 and fresh v3 toolboxes), so it is intentionally
   NOT bound to the v3 toolboxes; the reliable native sandbox builds the
   formula-driven ``.xlsx`` / ``.pptx`` deliverables instead.

4. **Artifacts egress server-side to Blob Storage.** The ``ResponsesHostServer``
   HTTP wrapper passes text content but strips code_interpreter file citations /
   annotations, and shipping files as base64-in-text is unreliable (the model
   resists large base64 and Azure content-filters the blob). Instead an
   :class:`~fsi_artifact_egress.ArtifactEgressMiddleware` runs inside
   ``agent.run(stream=False)``, harvests the code_interpreter output files
   in-container (where the sandbox ``container_id`` is still present), uploads them
   to the private ``artifacts`` blob container with the agent's managed identity,
   and appends one sentinel line per file to the response text::

       <<<ARTIFACT name=model.xlsx blob=artifacts/<run>/model.xlsx>>>

   The thin BFF parses these sentinels, streams the blob privately over
   ``/api/artifacts/...`` (no public access, no SAS), and strips the line from the
   text shown to the user. **The BFF must invoke the deployed agent with
   ``stream=False``** so the middleware can observe the full response.

**Skill tool approval:** :class:`~agent_framework.SkillsProvider` registers its tools
(``load_skill`` et al.) with ``approval_mode="always_require"``. The canonical fix is a
:class:`~agent_framework.ToolApprovalMiddleware`, but that middleware **requires an
``AgentSession``** — and the deployed :class:`ResponsesHostServer` invokes the agent
(``agent.run(stream=False, ...)``) **without** a session, so it raises
``ToolApprovalMiddleware requires an AgentSession``. Instead we disable approval on the
provider's tools directly (``approval_mode=None``, the FunctionTool default) so skill
loads execute inline with no approval round-trip and no session — safe for unattended
hosting (equivalent to auto-approving every skill tool).

Config is entirely env-driven so one image serves all three scenarios:

  FOUNDRY_PROJECT_ENDPOINT         project endpoint
  AZURE_AI_MODEL_DEPLOYMENT_NAME   e.g. gpt-5.1
  TOOLBOX_ENDPOINT                 full toolbox MCP URL (preferred), OR
  TOOLBOX_NAME                     toolbox name (endpoint built from project + name)
  STORAGE_BLOB_ENDPOINT            blob endpoint for artifact egress (optional locally)
  ARTIFACTS_CONTAINER              blob container name (default "artifacts")
  SEC_EDGAR_MCP_URL                full URL of the self-hosted sec-edgar-mcp server (/mcp)
  FSI_MCP_KEY                      shared secret the SEC MCP server expects
  FSI_MCP_KEY_HEADER               header carrying the secret (default x-fsi-mcp-key)
  SEC_EDGAR_DEEP_TOOLS             "true" to also allow slower full filing/statement tools
  FSI_SCENARIO_TITLE               human title for the system prompt
  FSI_SCENARIO_BRIEF               one-line description of the scenario's job
  FSI_STORE                        "true"/"false" response-store flag (default false)
  PORT                             host server port (default 8088)
"""
from __future__ import annotations

import logging
import os
from typing import Final

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import FoundryToolbox, ResponsesHostServer
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

try:  # package-relative when imported as a module
    from .fsi_artifact_egress import ArtifactEgressMiddleware
except ImportError:  # script / same-dir execution
    from fsi_artifact_egress import ArtifactEgressMiddleware

# Work around a core-1.10.0 observability regression: the agent-invocation span
# attribute builder eagerly json.dumps the tool params and chokes on the native
# code_interpreter param (``AutoCodeInterpreterToolParam`` is not JSON
# serializable), which fires whenever instrumentation is on. The deployed
# ResponsesHostServer calls ``setup_observability()`` at startup (the container has
# APPLICATIONINSIGHTS_CONNECTION_STRING) which re-enables instrumentation, so a plain
# ``enable_instrumentation = False`` property write is overwritten. We use the STICKY
# module-level ``disable_instrumentation()`` (sets ``_user_disabled=True``) so any
# later enable is silently dropped unless someone calls ``enable_instrumentation(force=True)``.
from agent_framework.observability import (  # noqa: E402
    OBSERVABILITY_SETTINGS,
    disable_instrumentation,
)

try:
    disable_instrumentation()  # sticky: survives the host's setup_observability()
except Exception:  # pragma: no cover - defensive across sdk versions
    try:
        OBSERVABILITY_SETTINGS.enable_instrumentation = False
    except Exception:
        try:
            OBSERVABILITY_SETTINGS._enable_instrumentation = False  # type: ignore[attr-defined]
        except Exception:
            pass

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fsi.hosted.v3")

AI_SCOPE: Final = "https://ai.azure.com/.default"

# Compact SEC EDGAR tool allow-list surfaced to the agent from the self-hosted
# sec-edgar-mcp server. Kept small on purpose: broad/full filing extraction tools are
# slow and token-heavy. Deep tools are added when SEC_EDGAR_DEEP_TOOLS=true.
SEC_EDGAR_COMPACT_TOOLS: Final = [
    "get_cik_by_ticker",
    "get_company_info",
    "search_companies",
    "get_recent_filings",
    "get_key_metrics",
    "compare_periods",
]
SEC_EDGAR_DEEP_TOOLS: Final = [
    "get_financials",
    "get_filing_sections",
    "get_company_facts",
]

DISCLAIMER: Final = (
    "All outputs are AI-generated for demonstration only using synthetic or "
    "publicly available data. Not investment advice."
)

# Sentinel emitted (by the egress middleware) into the response text so the BFF can
# recover each artifact's original filename and private blob path.
ARTIFACT_SENTINEL: Final = "<<<ARTIFACT name={name} blob={blob}>>>"


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _system_prompt() -> str:
    title = os.environ.get("FSI_SCENARIO_TITLE", "Financial Services Analyst")
    brief = os.environ.get(
        "FSI_SCENARIO_BRIEF",
        "Produce institutional-quality financial analysis and deliverables.",
    )
    return (
        f"You are the {title} agent for a financial-services demo. {brief}\n\n"
        "You have a governed library of specialist skills available through your "
        "toolbox via the load_skill tool (each skill's name and description is "
        "listed for you). You also have a code_interpreter tool (a sandboxed Python "
        "environment with openpyxl and python-pptx), governed SEC EDGAR tools (loaded "
        "from your toolbox) for public-company filings and XBRL financials, and a "
        "web_search tool for broader grounding.\n\n"
        "Workflow for EVERY request:\n"
        "1. Decide which skill(s) apply and call load_skill to load their full "
        "instructions BEFORE doing the work. The text returned by load_skill is "
        "complete and self-contained — do NOT call read_skill_resource or try to "
        "open companion/reference files; work directly from the loaded text.\n"
        "2. Follow the loaded skill's methodology precisely — especially "
        "formulas-over-hardcodes, step-by-step verification, and the specified "
        "Excel/PowerPoint formatting conventions.\n"
        "3. For real public-company references, prefer the compact SEC EDGAR toolbox "
        "tools over web search when you need company metadata, recent 10-K/10-Q/8-K "
        "filing metadata, or selected XBRL metrics. Start with get_company_info and "
        "get_recent_filings; use get_key_metrics only for a small set of named "
        "metrics. Avoid broad/full filing extraction unless the user explicitly asks "
        "for filing sections. Always cite the SEC URL, form type, and filing date "
        "returned by the tool. Use web_search only for market context not available "
        "from filings.\n"
        "4. Use the code_interpreter tool to build the actual .xlsx / .pptx "
        "deliverable and save it under /mnt/data. Use real cell formulas, not "
        "hardcoded values. If a loaded skill says to write a Python script and run it "
        "with Bash or a local shell, adapt that step to code_interpreter Python; this "
        "hosted runtime has code_interpreter, not a separate Bash tool. "
        "Saving the file to /mnt/data is all that is required — the platform "
        "automatically captures every file you write there and delivers it to the "
        "portal for download. Do NOT merely invent or mention a sandbox:/mnt/data "
        "link unless code_interpreter actually ran and saved the file. Do NOT "
        "base64-encode files, and do NOT paste file contents into your reply.\n"
        "5. Give a concise executive summary of what you built and the key figures. "
        "You may reference the file by name; the download is handled automatically.\n\n"
        f"Always end your final answer with this disclaimer: {DISCLAIMER}"
    )


def _disable_skill_tool_approval(provider) -> None:
    """Make a :class:`SkillsProvider`'s tools execute without approval.

    The provider builds its tools lazily in ``_create_tools`` (called from
    ``before_run``) with ``approval_mode="always_require"``. We wrap that bound
    method so every returned tool has ``approval_mode=None`` (the FunctionTool
    default = no approval). This lets skill loads run inline in the deployed
    ResponsesHostServer, which invokes the agent without an AgentSession and so
    cannot use ToolApprovalMiddleware.
    """
    original = provider._create_tools

    def _no_approval(skills):
        tools = original(skills)
        for tool in tools:
            tool.approval_mode = None
        return tools

    provider._create_tools = _no_approval


def _sec_edgar_mcp_tool(client):
    """Build the Foundry-native hosted (remote) SEC EDGAR MCP tool, or None.

    Returns ``client.get_mcp_tool(...)`` pointing at the self-hosted sec-edgar-mcp
    Container App. This is a SERVER-SIDE hosted MCP tool: the Foundry Responses
    gateway connects to the remote server and injects our shared-secret header at
    request time, so it coexists cleanly with the native code_interpreter/web_search
    tools (unlike a client-side toolbox load_tools=True surface, which suppresses the
    native hosted tools). The same server is also registered in the scenario toolbox
    (governed catalog); this is the runtime consumption path.

    Config (env):
      SEC_EDGAR_MCP_URL     full MCP URL of the hosted server (e.g. https://.../mcp)
      FSI_MCP_KEY           shared secret expected by the server middleware
      FSI_MCP_KEY_HEADER    header name carrying the secret (default x-fsi-mcp-key)
      SEC_EDGAR_DEEP_TOOLS  "true" to also allow slower full-filing/full-statement tools
    Returns None (SEC disabled) if SEC_EDGAR_MCP_URL is not set.
    """
    url = os.environ.get("SEC_EDGAR_MCP_URL", "").strip()
    if not url:
        logger.warning("SEC_EDGAR_MCP_URL not set; SEC EDGAR MCP tool disabled")
        return None
    key = os.environ.get("FSI_MCP_KEY", "").strip()
    header = os.environ.get("FSI_MCP_KEY_HEADER", "x-fsi-mcp-key").strip()
    headers = {header: key} if key else None

    allowed = list(SEC_EDGAR_COMPACT_TOOLS)
    if _env_bool("SEC_EDGAR_DEEP_TOOLS", default=False):
        allowed += SEC_EDGAR_DEEP_TOOLS

    return client.get_mcp_tool(
        name="sec_edgar",
        url=url,
        description=(
            "SEC EDGAR public-company filings and XBRL financials (self-hosted "
            "sec-edgar-mcp server). Use for company metadata, recent 10-K/10-Q/8-K "
            "filing metadata, and selected XBRL metrics."
        ),
        approval_mode="never_require",
        allowed_tools=allowed,
        headers=headers,
    )


def build_agent(credential: DefaultAzureCredential | None = None) -> tuple[Agent, FoundryToolbox]:
    """Construct the scenario hosted agent from environment configuration.

    Returns the agent and its toolbox. The caller MUST enter the returned toolbox as an
    async context manager (``async with toolbox:``) before serving requests: the toolbox
    is NOT in the agent's ``tools`` list (that would suppress native code_interpreter),
    so its MCP session is not connected by the agent's own lifecycle. Connecting it via
    the context manager is what makes ``as_skills_provider()`` skill discovery work.
    """
    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    model = os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"]
    cred = credential or DefaultAzureCredential()
    blob_endpoint = os.environ.get("STORAGE_BLOB_ENDPOINT")

    # Toolbox MCP: consumed for SKILLS ONLY via as_skills_provider() -> load_skill
    # progressive disclosure over MCP. load_tools=False is REQUIRED: setting it True to
    # surface the toolbox's bound MCP tools as client-side FunctionTools suppresses the
    # native hosted code_interpreter/web_search tools (the model can no longer call them
    # and hallucinates file creation). So the toolbox stays the governed skills + SEC
    # catalog, skills load natively over MCP, and the SEC EDGAR MCP server is consumed at
    # runtime as a Foundry-native hosted (remote) MCP tool built below — which coexists
    # cleanly with the native code_interpreter/web_search hosted tools.
    toolbox = FoundryToolbox(cred, token_scope=AI_SCOPE, load_tools=False)

    client = FoundryChatClient(project_endpoint=endpoint, model=model, credential=cred)

    # SkillsProvider registers load_skill/read_skill_resource/run_skill_script with
    # approval_mode="always_require". The canonical auto-approver, ToolApprovalMiddleware,
    # persists approval state in the AgentSession and raises "requires an AgentSession"
    # when context.session is None -- which is exactly how the deployed ResponsesHostServer
    # invokes us (agent.run(stream=False, ...) with no session). So we disable approval on
    # the provider's tools directly (approval_mode=None, the FunctionTool default): skills
    # then load inline with no approval round-trip and no session. Equivalent to
    # auto-approving every skill tool, which is what we want for unattended hosting.
    skills_provider = toolbox.as_skills_provider()
    _disable_skill_tool_approval(skills_provider)

    # SEC EDGAR = Foundry-native hosted (remote) MCP tool. The gateway connects to our
    # self-hosted sec-edgar-mcp Container App and injects the shared-secret header; no SEC
    # code runs in this agent image. None if SEC_EDGAR_MCP_URL is unset (SEC disabled).
    sec_mcp = _sec_edgar_mcp_tool(client)

    # The FoundryToolbox is consumed ONLY as a skills provider (load_skill progressive
    # disclosure over MCP). It is deliberately NOT placed in the agent's `tools` list:
    # doing so surfaces it to the Foundry Responses gateway as a hosted MCP server, which
    # suppresses the native code_interpreter/web_search hosted tools (the model then
    # hallucinates file creation and never runs code_interpreter). as_skills_provider()
    # still needs the toolbox CONNECTED for skill discovery, so the lifecycle owner
    # (main() / tests) enters the toolbox as an async context manager before the agent
    # runs -- exactly the alternative the SDK error message prescribes.
    tools: list = [
        client.get_code_interpreter_tool(),
        client.get_web_search_tool(),
    ]
    if sec_mcp is not None:
        tools.append(sec_mcp)

    agent = Agent(
        client=client,
        instructions=_system_prompt(),
        # Native code_interpreter builds the .xlsx/.pptx deliverables, native web_search
        # grounds facts, and (optionally) the hosted SEC EDGAR remote-MCP tool provides
        # filings. The toolbox is intentionally absent here (see the tools comment above):
        # it is connected out-of-band by the lifecycle owner so its skills can be
        # discovered without being exposed as a hosted MCP tool that suppresses native CI.
        tools=tools,
        # Skills are consumed natively off the same toolbox over MCP.
        context_providers=[skills_provider],
        # The egress middleware runs OUTERMOST so it observes the fully-materialised
        # response (with the CI container id) and can harvest + upload artifacts before
        # client conversion. NO ToolApprovalMiddleware: it needs a session the host does
        # not provide (see _disable_skill_tool_approval above).
        middleware=[
            ArtifactEgressMiddleware(
                project_endpoint=endpoint,
                blob_endpoint=blob_endpoint,
                credential=cred,
            ),
        ],
        # The hosting server owns response/thread state, so store=False matches the
        # canonical hosted samples. Overridable via FSI_STORE for experimentation.
        default_options={"store": _env_bool("FSI_STORE", default=False)},
    )
    return agent, toolbox


async def main() -> None:
    agent, toolbox = build_agent()
    port = int(os.environ.get("PORT", "8088"))
    logger.info(
        "starting FSI hosted agent '%s' on :%d",
        os.environ.get("FSI_SCENARIO_TITLE", "?"),
        port,
    )
    # Connect the toolbox MCP session for the whole server lifetime WITHOUT putting the
    # toolbox in the agent's tools list. This satisfies as_skills_provider()'s "toolbox
    # must be connected" requirement (so load_skill works) while keeping the toolbox off
    # the Responses tool surface, so the native code_interpreter/web_search hosted tools
    # are not suppressed.
    async with toolbox:
        server = ResponsesHostServer(agent)
        await server.run_async(port=port)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
