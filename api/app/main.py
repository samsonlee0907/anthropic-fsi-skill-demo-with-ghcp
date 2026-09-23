"""FastAPI backend for the FSI multi-agent demo portal."""
import json
import logging
import urllib.request

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from azure.identity import DefaultAzureCredential

from .config import (
    AGENT_NAMES, DEFAULT_PROMPTS, EDGAR_PROMPTS, ENVIRONMENT_NAME,
    MODEL_DEPLOYMENT_NAME, PROJECT_ENDPOINT, SCENARIOS, TOOLBOX_META,
)
from .orchestrator import ArtifactStorageUnavailable, resolve_artifact, run_scenario
from .webiq import enriched_run
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


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # Never echo request input back when a WebIQ credential accompanied it.
    if request.headers.get("x-webiq-key"):
        return JSONResponse(status_code=422, content={"detail": "Invalid workflow request. Check the input fields."})
    return await request_validation_exception_handler(request, exc)


class RunRequest(BaseModel):
    scenario: str
    message: str | None = None
    webiq_query: str | None = None


@app.get("/api/health")
def health():
    return {
        "status": "ok", "project_endpoint": PROJECT_ENDPOINT, "telemetry": _TELEMETRY_ON,
        "environment_name": ENVIRONMENT_NAME, "model_deployment_name": MODEL_DEPLOYMENT_NAME,
    }


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
def run(req: RunRequest, request: Request):
    key = request.headers.get("x-webiq-key", "")
    # Container Apps terminates TLS at its ingress proxy.
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    if key.strip() and not https:
        raise HTTPException(status_code=400, detail="WebIQ requires an HTTPS connection.")
    if req.scenario not in SCENARIOS:
        raise HTTPException(status_code=400, detail="Unknown scenario.")
    message = req.message or DEFAULT_PROMPTS.get(req.scenario, "")
    return StreamingResponse(
        enriched_run(req.scenario, message, key, req.webiq_query, run_scenario),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/api/artifacts/{artifact_id}")
def artifact(artifact_id: str):
    try:
        meta = resolve_artifact(artifact_id)
    except ArtifactStorageUnavailable:
        logging.getLogger(__name__).warning("Artifact download storage unavailable")
        raise HTTPException(status_code=503, detail="Artifact storage is temporarily unavailable.") from None
    if not meta:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(meta["path"], media_type=meta["media_type"], filename=meta["filename"])
