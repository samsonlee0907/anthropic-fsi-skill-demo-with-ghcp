"""Backend orchestration for the FSI multi-agent demo (DEPLOYED hosted agents).

Each scenario is served by ONE Foundry HOSTED agent in the target project. For a
run the BFF invokes the agent's Responses endpoint in **background mode** (POST with
``background=true`` returns immediately, then we poll ``GET .../responses/{id}`` until the
status is ``completed``). Background mode is required because long code_interpreter runs
exceed the Foundry gateway's non-streaming connection window (plain ``stream=false`` gets a
``RemoteProtocolError`` disconnect).

The hosted agent's ArtifactEgressMiddleware uploads any generated .xlsx/.pptx to the private
``artifacts`` blob container and appends a ``<<<ARTIFACT name=<f> blob=<container>/<path>>>>``
sentinel to the response text. This orchestrator parses those sentinels, downloads the blobs
privately (managed identity, no SAS), registers them for ``/api/artifacts/{id}`` download,
and strips the sentinel lines from the text shown to the user.

Progress UX: because a background run does not expose partial output (the stored
response's ``output`` stays empty until completion, and code_interpreter items are
stripped from the outer response), the BFF cannot forward a real token stream. To
avoid a static spinner it emits ``activity`` SSE events in two ways: time-based
lifecycle *phases* while the run is in flight, and the REAL tool calls (governed
skill loads + SEC EDGAR MCP calls) parsed from the completed payload.
"""
import asyncio
import json
import re
import tempfile
import time
import uuid
import zipfile
from functools import lru_cache
from pathlib import Path
from xml.sax.saxutils import escape
from typing import AsyncIterator, Dict

import httpx
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

from .config import (
    AGENT_NAMES,
    ARTIFACTS_CONTAINER,
    DATA_CONTEXT,
    DISCLAIMER,
    SCENARIOS,
    STORAGE_BLOB_ENDPOINT,
    agent_responses_base,
)
from .telemetry import get_tracer

_tracer = get_tracer()
_AI_SCOPE = "https://ai.azure.com/.default"

# id -> {"path": str, "filename": str, "media_type": str}
ARTIFACTS: Dict[str, dict] = {}
_ARTIFACT_DIR = Path(tempfile.gettempdir()) / "fsi_artifacts"
_ARTIFACT_DIR.mkdir(exist_ok=True)

_SENTINEL_RE = re.compile(r"^<<<ARTIFACT name=(?P<name>[^>]+?) blob=(?P<blob>[^>]*)>>>\s*$", re.M)

_MEDIA = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".csv": "text/csv",
    ".pdf": "application/pdf",
    ".png": "image/png",
}

_POLL_INTERVAL_S = 5
_POLL_TIMEOUT_S = 900
_POLL_HTTP_RETRIES = 5
_POLL_HTTP_BACKOFF_S = 3
_POLL_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

_FALLBACK_NAMES = {
    "equity-research": "equity_research_agent_summary.xlsx",
    "ib-pitch": "ib_pitch_agent_summary.pptx",
    "pe-lbo": "pe_lbo_agent_summary.xlsx",
}


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


# Time-based lifecycle phases surfaced as `activity` events while the background
# run is in flight. The hosted runtime does NOT expose partial output during a
# background run (the stored response's `output` stays empty until it completes,
# and code_interpreter items are stripped from the outer response), so a real
# event stream is not available mid-run. These phases give the user a visible,
# continuously-advancing view of the typical run lifecycle instead of a static
# "Awaiting output" spinner. Real, per-tool activities (skill loads, SEC EDGAR
# MCP calls) are emitted from the completed payload once the run finishes.
_RUN_PHASES = (
    (0, "default", "Request accepted — the agent is starting up"),
    (6, "function_call", "Loading governed skills from the scenario toolbox"),
    (24, "default", "Reasoning over the scenario inputs and synthetic data"),
    (48, "code_interpreter_call", "Running the financial model in the code interpreter"),
    (100, "code_interpreter_call", "Composing the Office artifact (workbook / deck)"),
    (160, "default", "Finalizing the narrative and packaging the artifact"),
)


def _phase_activities(elapsed: int, emitted: set, agent_name: str) -> list:
    """Return SSE strings for any lifecycle phases newly crossed at ``elapsed``."""
    out = []
    for threshold, kind, label in _RUN_PHASES:
        if elapsed >= threshold and threshold not in emitted:
            emitted.add(threshold)
            out.append(_sse({"type": "activity", "agent": agent_name,
                             "kind": kind, "label": label}))
    return out


def _skill_arg(item: dict) -> str:
    """Extract the skill name from a load_skill function_call item's arguments."""
    args = item.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            return ""
    if isinstance(args, dict):
        for key in ("skill", "skill_id", "name", "id"):
            val = args.get(key)
            if val:
                return str(val)
    return ""


def _activities_from_payload(payload: dict, agent_name: str) -> list:
    """Derive REAL behind-the-scenes tool-call activities from a completed run.

    The outer Responses payload exposes function_call items (governed skill loads
    via progressive disclosure) and mcp_call items (the self-hosted SEC EDGAR
    remote MCP tool). code_interpreter items are stripped by the host, so those
    are covered by the time-based lifecycle phases instead.
    """
    out = []
    seen = set()
    skill_names = []
    n_skill_loads = 0
    for item in payload.get("output") or []:
        itype = item.get("type") or ""
        if itype == "function_call":
            name = item.get("name") or ""
            if name in ("load_skill", "load_skills"):
                # Aggregate all skill loads into a single activity below.
                n_skill_loads += 1
                sk = _skill_arg(item)
                if sk and sk not in skill_names:
                    skill_names.append(sk)
                continue
            detail = name or "tool"
            label = "Called a toolbox function"
            key = ("fn", detail)
        elif itype in ("mcp_call", "mcp_tool_call"):
            detail = item.get("name") or item.get("tool_name") or "filing lookup"
            label = "Queried SEC EDGAR (MCP)"
            key = ("mcp", detail)
        elif "web_search" in itype:
            detail = ""
            label = "Ran a web search"
            key = ("web",)
        else:
            continue
        if key in seen:
            continue
        seen.add(key)
        event = {"type": "activity", "agent": agent_name, "kind": _KIND_FOR.get(itype, "default"),
                 "label": label}
        if detail:
            event["detail"] = detail
        out.append(_sse(event))

    if n_skill_loads:
        label = "Loaded governed skills" if n_skill_loads > 1 else "Loaded a governed skill"
        event = {"type": "activity", "agent": agent_name, "kind": "function_call", "label": label}
        if skill_names:
            event["detail"] = ", ".join(skill_names)
        elif n_skill_loads > 1:
            event["detail"] = f"{n_skill_loads} loaded"
        # Skill loads happen before tool calls in the turn; show them first.
        out.insert(0, _sse(event))
    return out


_KIND_FOR = {
    "function_call": "function_call",
    "mcp_call": "mcp_call",
    "mcp_tool_call": "mcp_call",
    "web_search_call": "web_search_call",
}


@lru_cache(maxsize=1)
def _credential() -> DefaultAzureCredential:
    return DefaultAzureCredential()


@lru_cache(maxsize=1)
def _blob_service() -> BlobServiceClient:
    return BlobServiceClient(account_url=STORAGE_BLOB_ENDPOINT, credential=_credential())


def _bearer() -> str:
    return _credential().get_token(_AI_SCOPE).token


def _register_artifact(fname: str, data: bytes) -> dict:
    art_id = uuid.uuid4().hex
    dest = _ARTIFACT_DIR / f"{art_id}_{fname}"
    dest.write_bytes(data)
    media = _MEDIA.get(Path(fname).suffix.lower(), "application/octet-stream")
    ARTIFACTS[art_id] = {"path": str(dest), "filename": fname, "media_type": media}
    return {"id": art_id, "filename": fname, "url": f"/api/artifacts/{art_id}"}


def _cell(row: int, value: str) -> str:
    safe = escape(value, {'"': '&quot;'})
    return f'<c r="A{row}" t="inlineStr"><is><t>{safe}</t></is></c>'


def _build_summary_workbook(scenario_key: str, text: str) -> bytes:
    """Create a tiny valid XLSX fallback when hosted artifact egress misses."""
    title = SCENARIOS.get(scenario_key, {}).get("title", scenario_key)
    lines = [
        "Fallback artifact generated by the API because the hosted agent completed "
        "without publishing a Blob artifact sentinel.",
        f"Scenario: {title}",
        "",
        "Agent narrative:",
        *(text or "(No narrative returned.)").splitlines(),
    ]
    rows = "\n".join(f'<row r="{idx}">{_cell(idx, line[:32000])}</row>' for idx, line in enumerate(lines, 1))
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<cols><col min="1" max="1" width="120" customWidth="1"/></cols>'
        f"<sheetData>{rows}</sheetData>"
        "</worksheet>"
    )
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Agent Summary" sheetId="1" r:id="rId1"/></sheets>'
            "</workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            "</Relationships>"
        ),
        "xl/styles.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
            '<borders count="1"><border/></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
            "</styleSheet>"
        ),
        "xl/worksheets/sheet1.xml": sheet,
    }
    dest = tempfile.SpooledTemporaryFile()
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, content in parts.items():
            z.writestr(name, content)
    dest.seek(0)
    return dest.read()


def _para(text: str, *, size: int = 1400, bold: bool = False) -> str:
    b = ' b="1"' if bold else ""
    safe = escape(text[:2000], {'"': '&quot;'})
    return (
        "<a:p><a:r>"
        f'<a:rPr lang="en-US" sz="{size}"{b}/>'
        f"<a:t>{safe}</a:t>"
        "</a:r></a:p>"
    )


def _build_summary_deck(scenario_key: str, text: str) -> bytes:
    """Create a minimal, valid single-slide PPTX fallback (no python-pptx needed).

    Used when the IB pitch agent completes without publishing a Blob deck artifact, so
    the delivered file type still matches the scenario (a .pptx deck, not a .xlsx)."""
    title = SCENARIOS.get(scenario_key, {}).get("title", scenario_key)
    body_lines = [
        "Fallback deck generated by the API because the hosted agent completed without "
        "publishing a Blob artifact sentinel.",
        "",
        *(text or "(No narrative returned.)").splitlines(),
    ]
    paras = _para(title, size=2400, bold=True) + "".join(_para(l) for l in body_lines if l is not None)

    A = "http://schemas.openxmlformats.org/drawingml/2006/main"
    R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    P = "http://schemas.openxmlformats.org/presentationml/2006/main"
    CT = "http://schemas.openxmlformats.org/package/2006/content-types"
    REL = "http://schemas.openxmlformats.org/package/2006/relationships"

    theme = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<a:theme xmlns:a="{A}" name="Office Theme"><a:themeElements>'
        '<a:clrScheme name="Office">'
        '<a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>'
        '<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>'
        '<a:dk2><a:srgbClr val="44546A"/></a:dk2><a:lt2><a:srgbClr val="E7E6E6"/></a:lt2>'
        '<a:accent1><a:srgbClr val="4472C4"/></a:accent1><a:accent2><a:srgbClr val="ED7D31"/></a:accent2>'
        '<a:accent3><a:srgbClr val="A5A5A5"/></a:accent3><a:accent4><a:srgbClr val="FFC000"/></a:accent4>'
        '<a:accent5><a:srgbClr val="5B9BD5"/></a:accent5><a:accent6><a:srgbClr val="70AD47"/></a:accent6>'
        '<a:hlink><a:srgbClr val="0563C1"/></a:hlink><a:folHlink><a:srgbClr val="954F72"/></a:folHlink>'
        '</a:clrScheme>'
        '<a:fontScheme name="Office">'
        '<a:majorFont><a:latin typeface="Calibri Light"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont>'
        '<a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont>'
        '</a:fontScheme>'
        '<a:fmtScheme name="Office">'
        '<a:fillStyleLst>'
        '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        '</a:fillStyleLst>'
        '<a:lnStyleLst>'
        '<a:ln w="6350"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
        '<a:ln w="12700"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
        '<a:ln w="19050"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
        '</a:lnStyleLst>'
        '<a:effectStyleLst>'
        '<a:effectStyle><a:effectLst/></a:effectStyle>'
        '<a:effectStyle><a:effectLst/></a:effectStyle>'
        '<a:effectStyle><a:effectLst/></a:effectStyle>'
        '</a:effectStyleLst>'
        '<a:bgFillStyleLst>'
        '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        '</a:bgFillStyleLst>'
        '</a:fmtScheme></a:themeElements></a:theme>'
    )
    empty_tree = (
        '<p:cSld><p:spTree>'
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr/></p:spTree></p:cSld>'
    )
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Types xmlns="{CT}">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
            '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
            '<Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
            '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
            '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
            '</Types>'
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{REL}">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>'
            '</Relationships>'
        ),
        "ppt/presentation.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<p:presentation xmlns:a="{A}" xmlns:r="{R}" xmlns:p="{P}">'
            '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
            '<p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>'
            '<p:sldSz cx="12192000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/>'
            '</p:presentation>'
        ),
        "ppt/_rels/presentation.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{REL}">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>'
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml"/>'
            '</Relationships>'
        ),
        "ppt/slideMasters/slideMaster1.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<p:sldMaster xmlns:a="{A}" xmlns:r="{R}" xmlns:p="{P}">'
            f'{empty_tree}'
            '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
            '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
            '</p:sldMaster>'
        ),
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{REL}">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>'
            '</Relationships>'
        ),
        "ppt/slideLayouts/slideLayout1.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<p:sldLayout xmlns:a="{A}" xmlns:r="{R}" xmlns:p="{P}" type="blank" preserve="1">'
            '<p:cSld name="Blank"><p:spTree>'
            '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
            '<p:grpSpPr/></p:spTree></p:cSld>'
            '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>'
            '</p:sldLayout>'
        ),
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{REL}">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>'
            '</Relationships>'
        ),
        "ppt/slides/slide1.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<p:sld xmlns:a="{A}" xmlns:r="{R}" xmlns:p="{P}"><p:cSld><p:spTree>'
            '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
            '<p:grpSpPr/>'
            '<p:sp><p:nvSpPr><p:cNvPr id="2" name="TextBox 1"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
            '<p:spPr><a:xfrm><a:off x="838200" y="365760"/><a:ext cx="10515600" cy="6096000"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
            f'<p:txBody><a:bodyPr wrap="square"><a:normAutofit/></a:bodyPr><a:lstStyle/>{paras}</p:txBody>'
            '</p:sp></p:spTree></p:cSld></p:sld>'
        ),
        "ppt/slides/_rels/slide1.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{REL}">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
            '</Relationships>'
        ),
        "ppt/theme/theme1.xml": theme,
    }
    dest = tempfile.SpooledTemporaryFile()
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, content in parts.items():
            z.writestr(name, content)
    dest.seek(0)
    return dest.read()


def _build_fallback_artifact(scenario_key: str, text: str) -> tuple[str, bytes]:
    """Return (filename, bytes) for the last-resort fallback, choosing a file type that
    matches the scenario's expected deliverable (deck for ib-pitch, workbook otherwise)."""
    name = _FALLBACK_NAMES.get(scenario_key, "agent_summary.xlsx")
    if name.lower().endswith(".pptx"):
        return name, _build_summary_deck(scenario_key, text)
    return name, _build_summary_workbook(scenario_key, text)


def _response_text(payload: dict) -> str:
    """Concatenate the assistant text from an OpenAI Responses payload."""
    chunks = []
    for item in payload.get("output", []) or []:
        if item.get("type") not in (None, "message"):
            continue
        for part in item.get("content", []) or []:
            if part.get("type") in ("output_text", "text"):
                chunks.append(part.get("text", ""))
    if not chunks and payload.get("output_text"):
        chunks.append(payload["output_text"])
    return "\n".join(chunks)


def _download_blob_sync(blobref: str) -> bytes:
    container, _, blobpath = blobref.partition("/")
    client = _blob_service().get_blob_client(container or ARTIFACTS_CONTAINER, blobpath)
    return client.download_blob().readall()


def _harvest_from_text_sync(text: str) -> tuple[str, list[dict]]:
    """Parse ARTIFACT sentinels, download each blob, return (clean_text, artifacts)."""
    artifacts: list[dict] = []
    for m in _SENTINEL_RE.finditer(text):
        name, blobref = m.group("name").strip(), m.group("blob").strip()
        if not blobref:
            artifacts.append({"id": None, "filename": name, "error": "no blob path"})
            continue
        try:
            data = _download_blob_sync(blobref)
            artifacts.append(_register_artifact(name, data))
        except Exception as e:  # noqa: BLE001
            artifacts.append({"id": None, "filename": name, "error": str(e)[:200]})
    clean = _SENTINEL_RE.sub("", text).strip()
    return clean, artifacts


def _artifact_retry_input(scenario_key: str, message: str, previous_text: str) -> str:
    return (
        _build_input(scenario_key, message)
        + "\n\nCORRECTIVE ARTIFACT TURN:\n"
        "The prior answer did not publish a downloadable portal artifact. It may have "
        "only mentioned a sandbox:/mnt/data link. That is not sufficient for this "
        "application. You MUST call the code_interpreter tool in this turn and create "
        "the requested Office file with Python under /mnt/data. If a loaded skill says "
        "to use Bash or a local shell, adapt that instruction to code_interpreter "
        "Python instead. Do not answer with only text, markdown tables, or a sandbox "
        "link. Save the file and then provide a concise summary.\n\n"
        f"Previous text-only answer for context:\n{previous_text[:2000]}"
    )


def _build_input(scenario_key: str, message: str) -> str:
    return (
        f"{DISCLAIMER}\n\n"
        f"USER REQUEST:\n{message}\n\n"
        f"SYNTHETIC DATASET (authoritative source for NovaGrid Technologies + peers; "
        f"code_interpreter has no internet, so use this data):\n{DATA_CONTEXT}\n\n"
        "SEC EDGAR guidance: if the user provides a real public ticker, keep filing "
        "retrieval compact. Use SEC company info and recent filing metadata first; "
        "request only a small set of named XBRL metrics when needed. Do not fetch full "
        "filing sections, full filing content, or broad financial statements unless "
        "the user explicitly asks for them, because those long retrievals can time out.\n\n"
        "Now do the work: (1) load the relevant skill(s); (2) you MUST call the "
        "code_interpreter tool to build the real workbook/deck file(s) with openpyxl / "
        "python-pptx and save them under /mnt/data — do NOT reproduce the model or slides "
        "as text tables in your reply; the saved file IS the deliverable. If a loaded "
        "skill says to use Bash or a local shell, adapt that step to code_interpreter "
        "Python because no Bash tool is available in this hosted runtime. Do not claim a "
        "sandbox:/mnt/data download link unless code_interpreter actually ran and saved "
        "the file. (3) Then give a "
        "short summary with the headline figures only."
    )


def _is_transient_timeout(error_text: str) -> bool:
    lowered = error_text.lower()
    return (
        "408" in lowered
        or "timeout" in lowered
        or "operation was timeout" in lowered
        or "429" in lowered
        or "rate_limit" in lowered
        or "rate limit" in lowered
    )


def _submit_background_sync(base: str, input_text: str) -> dict:
    with httpx.Client(timeout=120) as c:
        r = c.post(
            f"{base}?api-version=v1",
            headers={"Authorization": f"Bearer {_bearer()}"},
            json={"input": input_text, "stream": False, "store": True, "background": True},
        )
        r.raise_for_status()
        return r.json()


def _poll_once_sync(base: str, response_id: str) -> dict:
    """Poll a stored background response once, tolerating transient gateway blips.

    The run is persisted server-side (``store=True``), so a transient 5xx/429 or a
    network error on the GET is safe to retry in place: retrying the poll does not
    resubmit the run, it just re-reads the stored status. Without this, a single
    cosmetic ``500`` from the Foundry gateway would kill an otherwise-healthy run.
    """
    url = f"{base}/{response_id}?api-version=v1"
    last_exc: Exception | None = None
    for attempt in range(_POLL_HTTP_RETRIES):
        try:
            with httpx.Client(timeout=60) as c:
                r = c.get(url, headers={"Authorization": f"Bearer {_bearer()}"})
                r.raise_for_status()
                return r.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if isinstance(exc, httpx.HTTPStatusError) and status not in _POLL_RETRYABLE_STATUS:
                raise
            last_exc = exc
            time.sleep(_POLL_HTTP_BACKOFF_S * (attempt + 1))
    raise last_exc if last_exc else RuntimeError("poll failed with no exception")


async def run_scenario(scenario_key: str, message: str) -> AsyncIterator[str]:
    """Async generator yielding SSE strings for a full deployed-agent run."""
    scenario = SCENARIOS.get(scenario_key)
    if not scenario:
        yield _sse({"type": "error", "message": f"Unknown scenario '{scenario_key}'"})
        return

    agent_name = AGENT_NAMES.get(scenario_key, f"fsi-{scenario_key}")
    base = agent_responses_base(scenario_key)

    scenario_span = _tracer.start_span("scenario.run")
    scenario_span.set_attribute("fsi.scenario", scenario_key)
    scenario_span.set_attribute("fsi.toolbox", scenario["toolbox"])
    scenario_span.set_attribute("fsi.agent", agent_name)
    yield _sse({"type": "status", "stage": "start", "scenario": scenario_key,
                "title": scenario["title"], "toolbox": scenario["toolbox"]})
    yield _sse({"type": "agent_start", "agent": agent_name, "role": "scenario",
                "label": scenario["title"]})

    n_artifacts = 0
    clean_text = ""
    had_error = False
    with _tracer.start_as_current_span("agent.turn") as span:
        span.set_attribute("fsi.agent", agent_name)
        span.set_attribute("fsi.role", "scenario")
        try:
            primary_input = _build_input(scenario_key, message)
            payload = {}
            for attempt in range(2):
                try:
                    yield _sse({"type": "status", "stage": "submitting",
                                "scenario": scenario_key})
                    submit = await asyncio.to_thread(
                        _submit_background_sync, base, primary_input
                    )
                    rid = submit.get("id")
                    status = submit.get("status")
                    if not rid:
                        raise RuntimeError(f"no response id from submit: {json.dumps(submit)[:300]}")

                    t0 = time.time()
                    payload = submit
                    emitted_phases = set()
                    for _s in _phase_activities(0, emitted_phases, agent_name):
                        yield _s
                    while status in ("queued", "in_progress"):
                        await asyncio.sleep(_POLL_INTERVAL_S)
                        elapsed = int(time.time() - t0)
                        for _s in _phase_activities(elapsed, emitted_phases, agent_name):
                            yield _s
                        yield _sse({"type": "status", "stage": "working",
                                    "scenario": scenario_key, "elapsed_s": elapsed})
                        if elapsed > _POLL_TIMEOUT_S:
                            raise TimeoutError(f"agent run exceeded {_POLL_TIMEOUT_S}s")
                        payload = await asyncio.to_thread(_poll_once_sync, base, rid)
                        status = payload.get("status")

                    if status != "completed":
                        err = payload.get("error") or {"message": f"status={status}"}
                        raise RuntimeError(f"agent run {status}: {json.dumps(err)[:300]}")
                    break
                except Exception as exc:  # noqa: BLE001
                    if attempt == 0 and _is_transient_timeout(str(exc)):
                        yield _sse({"type": "status", "stage": "retrying",
                                    "scenario": scenario_key})
                        await asyncio.sleep(20)
                        continue
                    raise

            # Surface the REAL tool calls that fired during the primary run
            # (governed skill loads + SEC EDGAR MCP calls). code_interpreter items
            # are stripped by the host, so they are represented by the phases above.
            for _s in _activities_from_payload(payload, agent_name):
                yield _s

            raw_text = _response_text(payload)
            clean_text, artifacts = await asyncio.to_thread(_harvest_from_text_sync, raw_text)

            if not any(art.get("id") for art in artifacts):
                yield _sse({"type": "status", "stage": "ensuring_artifact",
                            "scenario": scenario_key})
                yield _sse({"type": "activity", "agent": agent_name,
                            "kind": "code_interpreter_call",
                            "label": "Re-running to materialize the artifact"})
                try:
                    submit = await asyncio.to_thread(
                        _submit_background_sync,
                        base,
                        _artifact_retry_input(scenario_key, message, clean_text or raw_text),
                    )
                    rid = submit.get("id")
                    status = submit.get("status")
                    if not rid:
                        raise RuntimeError(f"no response id from artifact retry: {json.dumps(submit)[:300]}")

                    t0 = time.time()
                    payload = submit
                    emitted_phases = set()
                    while status in ("queued", "in_progress"):
                        await asyncio.sleep(_POLL_INTERVAL_S)
                        elapsed = int(time.time() - t0)
                        for _s in _phase_activities(elapsed, emitted_phases, agent_name):
                            yield _s
                        yield _sse({"type": "status", "stage": "working",
                                    "scenario": scenario_key, "elapsed_s": elapsed})
                        if elapsed > _POLL_TIMEOUT_S:
                            raise TimeoutError(f"artifact retry exceeded {_POLL_TIMEOUT_S}s")
                        payload = await asyncio.to_thread(_poll_once_sync, base, rid)
                        status = payload.get("status")

                    if status != "completed":
                        err = payload.get("error") or {"message": f"status={status}"}
                        raise RuntimeError(f"artifact retry {status}: {json.dumps(err)[:300]}")

                    retry_text = _response_text(payload)
                    retry_clean, retry_artifacts = await asyncio.to_thread(
                        _harvest_from_text_sync, retry_text
                    )
                    if any(art.get("id") for art in retry_artifacts):
                        clean_text, artifacts = retry_clean, retry_artifacts
                    else:
                        clean_text = retry_clean or clean_text
                except Exception as exc:  # noqa: BLE001
                    span.set_attribute("fsi.artifact_retry_error", str(exc)[:300])

            if not any(art.get("id") for art in artifacts):
                fb_name, fb_bytes = _build_fallback_artifact(scenario_key, clean_text)
                artifacts = [_register_artifact(fb_name, fb_bytes)]

            # Deliver the (sentinel-stripped) narrative as a single delta.
            if clean_text:
                yield _sse({"type": "delta", "agent": agent_name, "text": clean_text})

            for art in artifacts:
                if art.get("id"):
                    n_artifacts += 1
                yield _sse({"type": "artifact", "agent": agent_name, **art})
        except Exception as e:  # noqa: BLE001
            had_error = True
            span.set_attribute("fsi.error", str(e)[:300])
            yield _sse({"type": "error", "agent": agent_name, "message": str(e)[:300]})
        span.set_attribute("fsi.artifacts", n_artifacts)
        span.set_attribute("fsi.chars", len(clean_text))
        span.set_attribute("fsi.ok", not had_error)

    yield _sse({"type": "agent_end", "agent": agent_name})
    scenario_span.end()
    yield _sse({"type": "done"})
