"""Backend orchestrator-worker pipeline for the FSI multi-agent demo.

For a chosen scenario it runs the specialist agents in order (each a Foundry
prompt agent invoked via the OpenAI-compatible responses API), streams their
output as Server-Sent Events, harvests any code_interpreter file artifacts, then
runs the scenario orchestrator agent to synthesize a final package.
"""
import json
import tempfile
import time
import uuid
from pathlib import Path
from typing import Dict, Iterator, List

from .config import DATA_CONTEXT, DISCLAIMER, SCENARIOS
from .foundry import get_openai_client
from .telemetry import get_tracer

_tracer = get_tracer()

# id -> {"path": str, "filename": str, "media_type": str}
ARTIFACTS: Dict[str, dict] = {}
_ARTIFACT_DIR = Path(tempfile.gettempdir()) / "fsi_artifacts"
_ARTIFACT_DIR.mkdir(exist_ok=True)

_MEDIA = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".csv": "text/csv",
    ".pdf": "application/pdf",
    ".png": "image/png",
}


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


def _download_artifacts(response, seen: set) -> List[dict]:
    """Extract + persist code_interpreter container files referenced in a response."""
    out: List[dict] = []
    oai = get_openai_client()
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", "") != "message":
            continue
        for content in getattr(item, "content", []) or []:
            for ann in getattr(content, "annotations", []) or []:
                if getattr(ann, "type", "") != "container_file_citation":
                    continue
                cid = getattr(ann, "container_id", None)
                fid = getattr(ann, "file_id", None)
                fname = getattr(ann, "filename", None) or f"{fid}.bin"
                key = (cid, fid)
                if not cid or not fid or key in seen:
                    continue
                seen.add(key)
                try:
                    binary = oai.containers.files.content.retrieve(fid, container_id=cid)
                    data = binary.content if hasattr(binary, "content") else binary.read()
                    art_id = uuid.uuid4().hex
                    dest = _ARTIFACT_DIR / f"{art_id}_{fname}"
                    dest.write_bytes(data)
                    media = _MEDIA.get(Path(fname).suffix.lower(), "application/octet-stream")
                    ARTIFACTS[art_id] = {"path": str(dest), "filename": fname, "media_type": media}
                    out.append({"id": art_id, "filename": fname, "url": f"/api/artifacts/{art_id}"})
                except Exception as e:  # noqa: BLE001
                    out.append({"id": None, "filename": fname, "error": str(e)[:200]})
    return out


def _stream_agent(agent_name: str, input_text: str, max_retries: int = 4):
    """Generator yielding event dicts as the agent streams.

    Yields: {"kind":"delta","text":str}, {"kind":"ping"} (heartbeat during long
    tool execution), and finally {"kind":"final","text":str,"response":obj} or
    {"kind":"error","message":str}. Retries transient errors with backoff.
    """
    oai = get_openai_client()
    attempt = 0
    while True:
        chunks: List[str] = []
        final_response = None
        last_ping = time.time()
        try:
            stream = oai.responses.create(
                input=input_text,
                stream=True,
                extra_body={"agent_reference": {"name": agent_name, "type": "agent_reference"}},
            )
            for ev in stream:
                etype = getattr(ev, "type", "")
                if etype == "response.output_text.delta":
                    delta = getattr(ev, "delta", "")
                    chunks.append(delta)
                    yield {"kind": "delta", "text": delta}
                elif etype == "response.completed":
                    final_response = getattr(ev, "response", None)
                else:
                    # Heartbeat on any non-text event (e.g. tool execution progress)
                    # so bytes keep flowing and the ingress idle timeout never fires.
                    now = time.time()
                    if now - last_ping >= 10:
                        last_ping = now
                        yield {"kind": "ping"}
            yield {"kind": "final", "text": "".join(chunks), "response": final_response}
            return
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            transient = ("rate limit" in msg or "429" in msg or "exceeded" in msg
                         or "timeout" in msg or "503" in msg or "500" in msg)
            attempt += 1
            if not transient or attempt > max_retries:
                yield {"kind": "error", "message": str(e)[:300]}
                return
            time.sleep(min(2 ** attempt * 5, 60))


def _run_agent(agent: str, role: str, label: str, input_text: str, seen_files: set) -> Iterator:
    """Yield SSE strings for one agent turn; the final tuple carries collected text."""
    yield _sse({"type": "agent_start", "agent": agent, "role": role, "label": label})
    full_text = ""
    n_deltas = 0
    n_artifacts = 0
    had_error = False
    with _tracer.start_as_current_span("agent.turn") as span:
        span.set_attribute("fsi.agent", agent)
        span.set_attribute("fsi.role", role)
        for evt in _stream_agent(agent, input_text):
            kind = evt["kind"]
            if kind == "delta":
                n_deltas += 1
                yield _sse({"type": "delta", "agent": agent, "text": evt["text"]})
            elif kind == "ping":
                yield ": keepalive\n\n"  # SSE comment; ignored by the client parser
            elif kind == "error":
                had_error = True
                span.set_attribute("fsi.error", evt["message"][:300])
                yield _sse({"type": "error", "agent": agent, "message": evt["message"]})
            elif kind == "final":
                full_text = evt["text"]
                final = evt["response"]
                if final is not None:
                    for art in _download_artifacts(final, seen_files):
                        n_artifacts += 1
                        yield _sse({"type": "artifact", "agent": agent, **art})
        span.set_attribute("fsi.deltas", n_deltas)
        span.set_attribute("fsi.artifacts", n_artifacts)
        span.set_attribute("fsi.chars", len(full_text))
        span.set_attribute("fsi.ok", not had_error)
    yield _sse({"type": "agent_end", "agent": agent})
    yield ("__text__", full_text)


def run_scenario(scenario_key: str, message: str) -> Iterator[str]:
    """Generator yielding SSE strings for the full multi-agent run."""
    scenario = SCENARIOS.get(scenario_key)
    if not scenario:
        yield _sse({"type": "error", "message": f"Unknown scenario '{scenario_key}'"})
        return

    seen_files: set = set()
    upstream_summaries: List[str] = []
    scenario_span = _tracer.start_span("scenario.run")
    scenario_span.set_attribute("fsi.scenario", scenario_key)
    scenario_span.set_attribute("fsi.toolbox", scenario["toolbox"])
    yield _sse({"type": "status", "stage": "start", "scenario": scenario_key,
                "title": scenario["title"], "toolbox": scenario["toolbox"]})

    for step in scenario["steps"]:
        agent = step["agent"]
        context = "\n\n".join(upstream_summaries) if upstream_summaries else "(none yet)"
        input_text = (
            f"ORCHESTRATED RUN. {DISCLAIMER}\n\n"
            f"USER REQUEST:\n{message}\n\n"
            f"SYNTHETIC DATASET (authoritative source for NovaGrid + peers):\n{DATA_CONTEXT}\n\n"
            f"UPSTREAM SPECIALIST OUTPUTS:\n{context}\n\n"
            "Deliver your specialist output now. Use code_interpreter to produce the real "
            "workbook/deck file(s) and reference them for download. Keep the written summary concise."
        )
        text = ""
        for out in _run_agent(agent, "specialist", step["label"], input_text, seen_files):
            if isinstance(out, tuple) and out[0] == "__text__":
                text = out[1]
            else:
                yield out
        summary = text[-1500:] if len(text) > 1500 else text
        upstream_summaries.append(f"[{agent}]\n{summary}")

    # Orchestrator synthesis.
    orch = scenario["orchestrator"]
    synth_input = (
        f"{DISCLAIMER}\n\nUSER REQUEST:\n{message}\n\n"
        f"SPECIALIST OUTPUTS TO SYNTHESIZE:\n" + "\n\n".join(upstream_summaries) + "\n\n"
        "Produce the final executive synthesis for the user: key conclusions, the triangulated "
        "numbers, assumptions, caveats, and a bullet list of the generated artifacts. Be concise."
    )
    for out in _run_agent(orch, "orchestrator", "Synthesizing final package", synth_input, seen_files):
        if not (isinstance(out, tuple) and out[0] == "__text__"):
            yield out
    scenario_span.end()
    yield _sse({"type": "done"})
