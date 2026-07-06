"""Create Foundry toolboxes for the FSI multi-agent demo via the data-plane REST API.

The `azd ai toolbox create` CLI omits `api-version=v1` + the `Foundry-Features`
header, so its POST routes to a legacy workspace path and returns
"WorkspaceNotFound". Calling the REST endpoint directly with the correct
api-version + feature header works. This script is idempotent: each run POSTs a
new version of each toolbox.

Endpoint: POST {PROJECT_ENDPOINT}/toolboxes/{name}/versions?api-version=v1
Headers : Authorization: Bearer <token for https://ai.azure.com/.default>
          Foundry-Features: Toolboxes=V1Preview
"""
import json
import os
import sys
import urllib.request
import urllib.error

from azure.identity import DefaultAzureCredential

PROJECT_ENDPOINT = os.environ.get(
    "PROJECT_ENDPOINT",
    "https://aif66lhnuec.services.ai.azure.com/api/projects/proj-fsi-demo",
).rstrip("/")

# Each scenario toolbox bundles Foundry-native tools the specialist agents share.
# code_interpreter -> build formula-driven .xlsx / .pptx (openpyxl / python-pptx)
# web_search       -> live grounding (SEC filings, market context)
# Vendor MCP data sources (FactSet, S&P Kensho, Daloopa, ...) are documented in
# the runbook as project connections that can be added here once credentials exist.
TOOLBOXES = {
    "tb-equity-research": {
        "description": "S1 Equity Research & Valuation: build DCF / comps / 3-statement models and ground with live web/SEC data.",
        "tools": [
            {"type": "code_interpreter"},
            {"type": "web_search", "name": "web"},
        ],
    },
    "tb-ib-pitch": {
        "description": "S2 Investment Banking Pitch: competitive analysis, PPTX deck authoring and deck QC with live grounding.",
        "tools": [
            {"type": "code_interpreter"},
            {"type": "web_search", "name": "web"},
        ],
    },
    "tb-pe-lbo": {
        "description": "S3 Private Equity LBO Screening: build LBO models and audit model integrity with live grounding.",
        "tools": [
            {"type": "code_interpreter"},
            {"type": "web_search", "name": "web"},
        ],
    },
}


def get_token() -> str:
    cred = DefaultAzureCredential()
    return cred.get_token("https://ai.azure.com/.default").token


def create_version(name: str, spec: dict, token: str) -> dict:
    url = f"{PROJECT_ENDPOINT}/toolboxes/{name}/versions?api-version=v1"
    data = json.dumps(spec).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Foundry-Features", "Toolboxes=V1Preview")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    token = get_token()
    ok = True
    for name, spec in TOOLBOXES.items():
        try:
            result = create_version(name, spec, token)
            print(f"[OK]   {name} -> version {result.get('version')} id={result.get('id')}")
        except urllib.error.HTTPError as e:
            ok = False
            body = e.read().decode("utf-8", "replace")
            print(f"[FAIL] {name} -> HTTP {e.code}: {body}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"[FAIL] {name} -> {e}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
