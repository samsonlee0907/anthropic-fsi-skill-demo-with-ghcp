"""FSI scenario hosted agent (v2, native skills) — Microsoft Agent Framework.

One hosted agent per FSI scenario. At startup it:

1. Downloads its scenario's Foundry skills (the Anthropic financial-analysis
   skills, registered by provision_skills.py) via the project `beta.skills` API,
   unpacks each into ``downloaded_skills/<name>/SKILL.md`` and wires them into a
   :class:`SkillsProvider`. The SDK advertises each skill's name+description and
   synthesizes a ``load_skill`` tool so the model loads full skill bodies on
   demand (progressive disclosure) — native skill consumption, no hand-injection.

2. Connects the Foundry-native **code_interpreter** tool (for building
   formula-driven .xlsx/.pptx via openpyxl / python-pptx) and the native
   **web_search** grounding tool, both provided by ``FoundryChatClient`` and
   executed server-side on the Foundry Responses sandbox.

.. note::
   The scenario **toolbox** (tb-*) also bundles ``code_interpreter`` + ``web``
   and has the scenario's skills bound to it, so it remains the governed,
   portal-visible, MCP-discoverable catalog. At runtime this agent uses the
   Foundry-native code_interpreter/web tools directly because the toolbox
   MCP-hosted ``code_interpreter`` tool currently returns a 500 ServerError in
   preview (``web`` and skill ``resources`` over that MCP endpoint work). Skills
   are consumed natively via progressive disclosure regardless.

Config is entirely env-driven so one module serves all three scenarios:

  FOUNDRY_PROJECT_ENDPOINT         project endpoint
  AZURE_AI_MODEL_DEPLOYMENT_NAME   e.g. gpt-5.1
  TOOLBOX_NAME                     e.g. tb-equity-research
  SKILL_NAMES                      comma-separated skills to load
  FSI_SCENARIO_TITLE               human title for the system prompt
  FSI_SCENARIO_BRIEF               one-line description of the scenario's job
"""
import asyncio
import io
import logging
import os
import shutil
import zipfile
from pathlib import Path
from typing import Final

from agent_framework import Agent, SkillsProvider
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fsi.hosted")

DOWNLOADED_SKILLS_DIR: Final = Path(__file__).parent / "downloaded_skills"
AI_SCOPE: Final = "https://ai.azure.com/.default"
SKILL_BOOTSTRAP_TIMEOUT_SECONDS: Final = 90.0

DISCLAIMER = (
    "All outputs are AI-generated for demonstration only using synthetic or "
    "publicly available data. Not investment advice."
)


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    root = dest.resolve()
    for m in zf.infolist():
        target = (root / m.filename).resolve()
        if root != target and root not in target.parents:
            raise RuntimeError(f"unsafe zip path: {m.filename}")
    zf.extractall(dest)


def _bootstrap_skills(endpoint: str, skill_names: list[str], target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    cred = DefaultAzureCredential()
    with AIProjectClient(endpoint=endpoint, credential=cred, allow_preview=True) as project:
        for name in skill_names:
            logger.info("downloading skill '%s'", name)
            stream = project.beta.skills.download(name)
            zip_bytes = b"".join(stream)
            skill_dir = target / name
            skill_dir.mkdir()
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                _safe_extract(zf, skill_dir)
            if not (skill_dir / "SKILL.md").is_file():
                raise RuntimeError(f"'{name}' archive missing SKILL.md at root")


def _system_prompt() -> str:
    title = os.environ.get("FSI_SCENARIO_TITLE", "Financial Services Analyst")
    brief = os.environ.get(
        "FSI_SCENARIO_BRIEF",
        "Produce institutional-quality financial analysis and deliverables.",
    )
    return (
        f"You are the {title} agent for a financial-services demo. {brief}\n\n"
        "You have a library of specialist skills available via the load_skill tool "
        "(each skill's name and description are listed for you). Workflow for every "
        "request:\n"
        "1. Decide which skill(s) apply and call load_skill to load their full "
        "instructions BEFORE doing the work.\n"
        "2. Follow the loaded skill's methodology precisely — especially "
        "formulas-over-hardcodes, step-by-step verification, and the specified "
        "Excel/PowerPoint formatting conventions.\n"
        "3. Use the code_interpreter tool to build the actual .xlsx / .pptx "
        "deliverable (openpyxl / python-pptx) and save it to /mnt/data. Use "
        "web_search only for grounding facts you don't have.\n"
        "4. Return a concise executive summary of what you built and the key "
        "figures.\n\n"
        f"Always end your final answer with this disclaimer: {DISCLAIMER}"
    )


async def main() -> None:
    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    model = os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"]
    skill_names = [s.strip() for s in os.environ.get("SKILL_NAMES", "").split(",") if s.strip()]

    # 1. Native skills via progressive disclosure.
    context_providers = []
    if skill_names:
        await asyncio.wait_for(
            asyncio.to_thread(_bootstrap_skills, endpoint, skill_names, DOWNLOADED_SKILLS_DIR),
            timeout=SKILL_BOOTSTRAP_TIMEOUT_SECONDS,
        )
        context_providers.append(SkillsProvider.from_paths(skill_paths=str(DOWNLOADED_SKILLS_DIR)))
        logger.info("loaded %d skills: %s", len(skill_names), skill_names)
    else:
        logger.warning("SKILL_NAMES empty — no skills loaded")

    # 2. Foundry-native tools executed on the Responses sandbox:
    #    code_interpreter builds the .xlsx/.pptx deliverables, web_search grounds
    #    facts. (The scenario toolbox still bundles/binds these + the skills as
    #    the governed catalog, but its MCP-hosted code_interpreter 500s in
    #    preview, so we use the reliable native tools here.)
    cred = DefaultAzureCredential()
    client = FoundryChatClient(project_endpoint=endpoint, model=model, credential=cred)
    tools = [
        client.get_code_interpreter_tool(),
        client.get_web_search_tool(),
    ]

    agent = Agent(
        client=client,
        instructions=_system_prompt(),
        tools=tools,
        context_providers=context_providers,
        default_options={"store": False},
    )

    server = ResponsesHostServer(agent)
    port = int(os.environ.get("PORT", "8088"))
    await server.run_async(port=port)


if __name__ == "__main__":
    asyncio.run(main())
