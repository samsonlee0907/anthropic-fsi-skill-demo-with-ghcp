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

2. **web_search and SEC EDGAR execute THROUGH the governed toolbox.** A second
   :class:`FoundryToolbox` connection (``load_tools=True``, ``allowed_tools`` restricted
   to ``web`` + ``sec_edgar___*``) is placed in the agent's ``tools`` list, so the model
   invokes web search and SEC EDGAR filings as client-side function tools whose
   execution is routed over the scenario toolbox MCP endpoint. The toolbox is therefore
   the single, unified, governed tool surface — not merely a catalog. The self-hosted
   ``sec-edgar-mcp`` server (``ca-secedgar-mcp-<env>``) is registered in each scenario
   toolbox as an ``mcp`` tool and reached this way; ``web_search`` is registered as the
   ``web`` tool. The mandatory ``Foundry-Features: Toolboxes=V1Preview`` header is set on
   the toolbox HTTP client.

3. **code_interpreter is the Foundry-native Responses sandbox tool**
   (``FoundryChatClient.get_code_interpreter_tool``), NOT the toolbox's, and is the ONE
   tool kept native. The toolbox-MCP ``code_interpreter`` tool returns a reproducible
   server-side ``500`` in preview (confirmed live on this project, even with
   ``container: {type: auto}`` and the ``Foundry-Features`` header), so it is excluded
   from the tools-toolbox ``allowed_tools`` and the reliable native sandbox builds the
   formula-driven ``.xlsx`` / ``.pptx`` deliverables instead. Excluding it also avoids a
   name collision that would let the broken toolbox ``code_interpreter`` shadow the
   working native one.

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
  AZURE_AI_MODEL_DEPLOYMENT_NAME   e.g. gpt-5.4
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

# Every toolbox MCP request must carry this header or the endpoint rejects the call
# ("Foundry-Features: Toolboxes=V1Preview"). FoundryToolbox's auth flow forwards the
# platform per-request headers but does not statically set this one, so the runtime adds
# it to the tools-toolbox HTTP client explicitly (belt-and-suspenders in the hosted env).
TOOLBOX_FEATURES_HEADER: Final = ("Foundry-Features", "Toolboxes=V1Preview")

# The web_search tool's name as bound in each scenario toolbox
# (bind_skills_to_toolboxes.py -> {"type": "web_search", "name": "web"}). MCP-sourced
# SEC tools are namespaced by the server_label with THREE underscores: sec_edgar___<tool>.
TOOLBOX_WEB_TOOL: Final = "web"

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
        "environment with openpyxl and python-pptx), governed SEC EDGAR tools "
        "(named sec_edgar___* and routed through your toolbox) for public-company "
        "filings and XBRL financials, and a web tool (web search) for broader "
        "grounding.\n\n"
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
        "filing metadata, or selected XBRL metrics. Start with "
        "sec_edgar___get_company_info and sec_edgar___get_recent_filings; use "
        "sec_edgar___get_key_metrics only for a small set of named metrics. Avoid "
        "broad/full filing extraction unless the user explicitly asks for filing "
        "sections. Always cite the SEC URL, form type, and filing date returned by "
        "the tool. Use the web tool only for market context not available from "
        "filings.\n"
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


async def _prewarm_skills(provider) -> None:
    """Fetch + cache every toolbox skill's body at startup, while the toolbox MCP
    session is freshly connected in THIS task.

    Why this exists: ``as_skills_provider()`` builds ``MCPSkill`` objects bound to the
    toolbox's long-lived MCP ``ClientSession`` (opened by ``async with skills_toolbox:`` in
    ``main()``). ``load_skill`` calls ``MCPSkill.get_content()``, which issues a
    ``resources/read`` on that session. In the deployed :class:`ResponsesHostServer` the
    per-request agent invocation runs in a DIFFERENT asyncio task than the one that
    opened the session, so that request-time ``resources/read`` fails and the tool
    returns the opaque ``"Error: Function failed."`` -- the model then gives up and
    hallucinates file creation instead of running code_interpreter.

    ``MCPSkill.get_content()`` caches its result (``self._content``) after the first
    successful read, and ``SkillsProvider`` caches its context (the same ``MCPSkill``
    instances) across runs when ``disable_caching=False`` (our default). So reading each
    skill's content ONCE here at startup -- in the same task that owns the session, which
    a direct MCP probe confirms works -- populates those caches. Every later
    ``load_skill`` then returns the cached body with no request-time session read, which
    is what makes skills actually usable in the hosted runtime.
    """
    get_ctx = getattr(provider, "_get_or_create_context", None)
    if get_ctx is None:  # pragma: no cover - SDK shape guard
        logger.warning("skill prewarm: provider has no _get_or_create_context; skipping")
        return
    try:
        skills, _instructions, _tools = await get_ctx()
    except Exception as e:  # noqa: BLE001
        logger.warning("skill prewarm: skill discovery failed: %s", e)
        return
    warmed = 0
    for skill in skills:
        name = getattr(getattr(skill, "frontmatter", None), "name", "?")
        try:
            body = await skill.get_content()
            warmed += 1
            logger.info("skill prewarm: cached '%s' (%d chars)", name, len(body or ""))
        except Exception as e:  # noqa: BLE001
            logger.warning("skill prewarm: '%s' failed: %s", name, e)
    logger.info("skill prewarm: %d/%d skill bodies cached", warmed, len(skills))


def _toolbox_tool_allowlist() -> set[str]:
    """Toolbox tool names whose EXECUTION the runtime routes through the toolbox MCP.

    We expose ``web_search`` (bound as ``web``) and the SEC EDGAR MCP tools
    (namespaced ``sec_edgar___<tool>``) as client-side function tools off the scenario
    toolbox, so their execution is governed by the toolbox — the single unified tool
    surface. ``code_interpreter`` is deliberately EXCLUDED from this allow-list: the
    preview toolbox-MCP ``code_interpreter`` returns a reproducible server-side ``500``
    (verified live on this project, even with ``container: {type: auto}`` and the
    ``Foundry-Features`` header), so it runs as the reliable Foundry-native hosted tool
    instead. Excluding it here also prevents a name collision in which the broken
    toolbox ``code_interpreter`` would shadow the working native one.
    """
    allow: set[str] = {TOOLBOX_WEB_TOOL}
    sec = list(SEC_EDGAR_COMPACT_TOOLS)
    if _env_bool("SEC_EDGAR_DEEP_TOOLS", default=False):
        sec += SEC_EDGAR_DEEP_TOOLS
    allow.update(f"sec_edgar___{t}" for t in sec)
    return allow


def build_agent(
    credential: DefaultAzureCredential | None = None,
) -> tuple[Agent, FoundryToolbox, object]:
    """Construct the scenario hosted agent from environment configuration.

    Returns ``(agent, skills_toolbox, skills_provider)``. The caller MUST enter the
    returned ``skills_toolbox`` as an async context manager (``async with``) before
    serving requests so ``as_skills_provider()`` skill discovery works (its MCP session
    is not otherwise connected). A SECOND toolbox connection (for web_search + SEC EDGAR
    execution) is placed in the agent's ``tools`` list and connected by the agent's own
    lifecycle — see the tools section below.
    """
    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    model = os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"]
    cred = credential or DefaultAzureCredential()
    blob_endpoint = os.environ.get("STORAGE_BLOB_ENDPOINT")

    client = FoundryChatClient(project_endpoint=endpoint, model=model, credential=cred)

    # --- Toolbox connection #1: SKILLS provider -------------------------------------
    # Consumed for SKILLS ONLY via as_skills_provider() -> load_skill progressive
    # disclosure over MCP (SEP-2640). load_tools=False keeps this connection's tools off
    # the agent surface; skill bodies are prewarmed at startup (see _prewarm_skills).
    skills_toolbox = FoundryToolbox(cred, token_scope=AI_SCOPE, load_tools=False)

    # SkillsProvider registers load_skill/read_skill_resource/run_skill_script with
    # approval_mode="always_require". The canonical auto-approver, ToolApprovalMiddleware,
    # persists approval state in the AgentSession and raises "requires an AgentSession"
    # when context.session is None -- which is exactly how the deployed ResponsesHostServer
    # invokes us (agent.run(stream=False, ...) with no session). So we disable approval on
    # the provider's tools directly (approval_mode=None, the FunctionTool default): skills
    # then load inline with no approval round-trip and no session. Equivalent to
    # auto-approving every skill tool, which is what we want for unattended hosting.
    skills_provider = skills_toolbox.as_skills_provider()
    _disable_skill_tool_approval(skills_provider)

    # --- Toolbox connection #2: TOOL execution (web_search + SEC EDGAR) --------------
    # A SECOND connection to the SAME scenario toolbox, consumed as client-side function
    # tools but restricted with allowed_tools to web_search (`web`) + the SEC EDGAR MCP
    # tools (`sec_edgar___*`). This routes their EXECUTION through the governed toolbox,
    # so the toolbox is the single unified, governed tool surface — not just a catalog.
    # code_interpreter is EXCLUDED from the allow-list (preview toolbox CI 500s), so it
    # keeps running as the Foundry-native hosted tool below, with no name collision.
    # FoundryToolbox does not accept allowed_tools, so we set it post-construction (it is
    # read at connect/load_tools time). We also add the mandatory Foundry-Features header.
    tools_toolbox = FoundryToolbox(cred, token_scope=AI_SCOPE, load_tools=True)
    tools_toolbox.allowed_tools = _toolbox_tool_allowlist()
    try:
        tools_toolbox._httpx_client.headers[TOOLBOX_FEATURES_HEADER[0]] = (
            TOOLBOX_FEATURES_HEADER[1]
        )
    except Exception:  # noqa: BLE001 - defensive; platform may already inject it
        logger.warning("could not set Foundry-Features header on tools-toolbox client")

    # Native code_interpreter builds the .xlsx/.pptx deliverables (the toolbox-MCP
    # code_interpreter 500s in preview). web_search + SEC EDGAR execute THROUGH the
    # toolbox (tools_toolbox). Placing tools_toolbox in `tools` makes the agent connect
    # and manage its MCP session for the agent's lifetime; only web/sec are exposed
    # (allow-list), so the native code_interpreter hosted tool is not shadowed.
    tools: list = [
        tools_toolbox,
        client.get_code_interpreter_tool(),
    ]

    agent = Agent(
        client=client,
        instructions=_system_prompt(),
        # tools_toolbox routes web_search + SEC EDGAR through the governed toolbox;
        # native code_interpreter builds the deliverables. The skills toolbox is NOT in
        # this list (see main()): it is connected out-of-band so its skills are discovered
        # without exposing a second, redundant tool surface.
        tools=tools,
        # Skills are consumed off the skills toolbox over MCP.
        context_providers=[skills_provider],
        # The egress middleware runs OUTERMOST so it observes the fully-materialised
        # response (with the native CI container id) and can harvest + upload artifacts
        # before client conversion. NO ToolApprovalMiddleware: it needs a session the host
        # does not provide (see _disable_skill_tool_approval above).
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
    return agent, skills_toolbox, skills_provider


async def main() -> None:
    agent, skills_toolbox, skills_provider = build_agent()
    port = int(os.environ.get("PORT", "8088"))
    logger.info(
        "starting FSI hosted agent '%s' on :%d",
        os.environ.get("FSI_SCENARIO_TITLE", "?"),
        port,
    )
    # Connect the SKILLS toolbox MCP session for the whole server lifetime. This
    # satisfies as_skills_provider()'s "toolbox must be connected" requirement (so
    # load_skill works). The web_search/SEC EDGAR tools toolbox is connected separately
    # by the agent (it is in the agent's tools list).
    async with skills_toolbox:
        # Pre-fetch + cache every skill body now, while the freshly-connected session is
        # owned by THIS task. Without this, the first request-time load_skill runs in a
        # different task and its MCP resources/read fails ("Error: Function failed."),
        # so the model hallucinates instead of using the skill + code_interpreter.
        await _prewarm_skills(skills_provider)
        server = ResponsesHostServer(agent)
        await server.run_async(port=port)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
