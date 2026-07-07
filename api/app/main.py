"""FastAPI backend for the FSI multi-agent demo portal."""
import json
import urllib.request

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from azure.identity import DefaultAzureCredential

from .config import AGENT_NAMES, DEFAULT_PROMPTS, EDGAR_PROMPTS, PROJECT_ENDPOINT, SCENARIOS, TOOLBOX_META
from .orchestrator import ARTIFACTS, run_scenario
from .telemetry import configure as configure_telemetry

_TELEMETRY_ON = configure_telemetry()

app = FastAPI(title="FSI Multi-Agent Demo API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_cred = DefaultAzureCredential()


class RunRequest(BaseModel):
    scenario: str
    message: str | None = None


@app.get("/api/health")
def health():
    return {"status": "ok", "project_endpoint": PROJECT_ENDPOINT, "telemetry": _TELEMETRY_ON}


@app.get("/api/scenarios")
def scenarios():
    out = []
    for key, s in SCENARIOS.items():
        out.append({
            "key": key,
            "title": s["title"],
            "tagline": s["tagline"],
            "toolbox": s["toolbox"],
            "agent": AGENT_NAMES.get(key, f"fsi-{key}"),
            "skills": s["skills"],
            "default_prompt": DEFAULT_PROMPTS.get(key, ""),
            "edgar_prompt": EDGAR_PROMPTS.get(key, ""),
        })
    return {"scenarios": out}


@app.get("/api/toolboxes")
def toolboxes():
    """List Foundry toolboxes (the reusable tool catalog powering the agents)."""
    try:
        token = _cred.get_token("https://ai.azure.com/.default").token
        req = urllib.request.Request(f"{PROJECT_ENDPOINT}/toolboxes?api-version=v1")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Foundry-Features", "Toolboxes=V1Preview")
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = []
        for tb in data.get("data", []):
            name = tb.get("name")
            meta = TOOLBOX_META.get(name, {})
            items.append({
                "name": name,
                "description": tb.get("description") or meta.get("description", ""),
                "tools": meta.get("tools", []),
            })
        return {"toolboxes": items}
    except Exception as e:  # noqa: BLE001
        return {"toolboxes": [], "error": str(e)[:200]}


@app.post("/api/run")
def run(req: RunRequest):
    if req.scenario not in SCENARIOS:
        raise HTTPException(status_code=400, detail=f"Unknown scenario '{req.scenario}'")
    message = req.message or DEFAULT_PROMPTS.get(req.scenario, "")
    return StreamingResponse(
        run_scenario(req.scenario, message),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/artifacts/{artifact_id}")
def artifact(artifact_id: str):
    meta = ARTIFACTS.get(artifact_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(meta["path"], media_type=meta["media_type"], filename=meta["filename"])
