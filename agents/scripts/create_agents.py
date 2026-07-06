"""Create the FSI multi-agent roster (8 specialists + 3 orchestrators) as Foundry prompt agents.

Design notes
------------
* Each specialist agent is a PROMPT agent whose instructions are the converted
  Anthropic SKILL.md content (agents/skills/*.md) and whose tool surface is
  attached DIRECTLY: CodeInterpreterTool (build formula-driven .xlsx/.pptx via
  openpyxl / python-pptx) + WebSearchPreviewTool (live grounding).
* The same tool surface is also published as reusable Foundry TOOLBOXES
  (tb-equity-research / tb-ib-pitch / tb-pe-lbo, see create_toolboxes.py) which
  the portal surfaces and the runbook demonstrates via their MCP endpoints.
  Native "agent references toolbox by name" is not yet available in the current
  preview SDK (the raw toolbox MCP URL path returns 424 external_connector_error
  because the agent runtime cannot inject an auth token to that endpoint), so the
  reliable, GA path is to attach the tools directly to each agent.
* Orchestration across specialists is implemented in the backend (orchestrator-
  worker pattern) for reliability + traceability; the orchestrator prompt agents
  own the synthesis instructions.

Idempotent: create_version adds a new version each run; the latest becomes default.
"""
import json
import os
import sys

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    PromptAgentDefinition,
    CodeInterpreterTool,
    WebSearchPreviewTool,
)

PROJECT_ENDPOINT = os.environ.get(
    "PROJECT_ENDPOINT",
    "https://aif66lhnuec.services.ai.azure.com/api/projects/proj-fsi-demo",
).rstrip("/")

SKILLS_DIR = os.path.join(os.path.dirname(__file__), "..", "skills")
MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "..", "definitions", "agents-manifest.json")

GPT = "gpt-5.1"
GPT_MINI = "gpt-5.4-mini"

# name -> (skill file, model, scenario, role)
SPECIALISTS = {
    "fsi-three-statement-agent": ("three-statement-agent.md", GPT, "equity-research", "specialist"),
    "fsi-dcf-agent": ("dcf-agent.md", GPT, "equity-research", "specialist"),
    "fsi-comps-agent": ("comps-agent.md", GPT, "equity-research", "specialist"),
    "fsi-competitive-analysis-agent": ("competitive-analysis-agent.md", GPT, "ib-pitch", "specialist"),
    "fsi-pptx-author-agent": ("pptx-author-agent.md", GPT, "ib-pitch", "specialist"),
    "fsi-deck-qc-agent": ("deck-qc-agent.md", GPT_MINI, "ib-pitch", "specialist"),
    "fsi-lbo-agent": ("lbo-agent.md", GPT, "pe-lbo", "specialist"),
    "fsi-model-audit-agent": ("model-audit-agent.md", GPT_MINI, "pe-lbo", "specialist"),
}

ORCHESTRATORS = {
    "fsi-orchestrator-equity-research": ("_orchestrator-equity-research.md", GPT, "equity-research", "orchestrator"),
    "fsi-orchestrator-ib-pitch": ("_orchestrator-ib-pitch.md", GPT, "ib-pitch", "orchestrator"),
    "fsi-orchestrator-pe-lbo": ("_orchestrator-pe-lbo.md", GPT, "pe-lbo", "orchestrator"),
}


def read_instructions(fname: str) -> str:
    path = os.path.join(SKILLS_DIR, fname)
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def build_definition(instructions: str, model: str, role: str) -> PromptAgentDefinition:
    # Specialists build artifacts + ground; orchestrators synthesize (code_interpreter
    # only, to assemble summary tables). Both share the same reliable GA tool classes.
    if role == "orchestrator":
        tools = [CodeInterpreterTool()]
    else:
        tools = [CodeInterpreterTool(), WebSearchPreviewTool()]
    return PromptAgentDefinition(model=model, instructions=instructions, tools=tools)


def main() -> int:
    client = AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=DefaultAzureCredential())
    manifest = {"project_endpoint": PROJECT_ENDPOINT, "agents": []}
    ok = True
    roster = {**SPECIALISTS, **ORCHESTRATORS}
    for name, (fname, model, scenario, role) in roster.items():
        try:
            instructions = read_instructions(fname)
            definition = build_definition(instructions, model, role)
            v = client.agents.create_version(agent_name=name, definition=definition)
            version = getattr(v, "version", None) or getattr(v, "id", None)
            manifest["agents"].append(
                {"name": name, "version": str(version), "model": model,
                 "scenario": scenario, "role": role, "skill_file": fname}
            )
            print(f"[OK]   {name} (v{version}) model={model} scenario={scenario} role={role}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"[FAIL] {name} -> {str(e)[:300]}")
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"\nManifest written: {MANIFEST_PATH} ({len(manifest['agents'])} agents)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
