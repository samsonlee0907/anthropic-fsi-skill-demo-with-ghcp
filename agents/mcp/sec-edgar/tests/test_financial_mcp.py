"""Exercise real FastMCP HTTP registration/auth/structured output; SEC HTTP is stubbed."""
import importlib
import json
from pathlib import Path
import sys

import pytest
from starlette.testclient import TestClient

pytest.importorskip("sec_edgar_mcp", reason="Remote MCP integration needs agents/mcp/sec-edgar/requirements.txt")

SERVER_DIR = Path(__file__).resolve().parents[1]


def test_normalized_tool_registered_alongside_upstream_and_returns_fact_pack(monkeypatch):
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "Financial regression tests tests@example.test")
    monkeypatch.setenv("FSI_MCP_KEY", "local-test-key-not-a-production-secret")
    monkeypatch.syspath_prepend(str(SERVER_DIR))
    for name in ("sec_edgar_http_server", "financial_fact_pack"):
        sys.modules.pop(name, None)
    server = importlib.import_module("sec_edgar_http_server")
    normalizer = importlib.import_module("financial_fact_pack")
    fixture = json.loads((Path(__file__).parent / "fixtures" / "financial_msft_sec_20260916.json").read_text())
    calls = []
    def fetch(url, user_agent):
        calls.append(url)
        return fixture["companyfacts"] if "companyfacts" in url else fixture["submissions"]
    monkeypatch.setattr(normalizer, "_fetch", fetch)
    headers = {"Accept": "application/json, text/event-stream",
               "x-fsi-mcp-key": "local-test-key-not-a-production-secret"}
    with TestClient(server.app) as client:
        assert client.get("/healthz").status_code == 200
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        assert client.post("/mcp", json=request).status_code == 401
        response = client.post("/mcp", json=request, headers=headers)
        assert response.status_code == 200, response.text
        tools = {x["name"]: x for x in response.json()["result"]["tools"]}
        assert "get_company_facts" in tools
        tool = tools["get_financial_fact_pack"]
        assert tool["inputSchema"]["required"] == ["ticker", "as_of"]
        assert "annual consolidated historical revenue" in tool["description"]
        response = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "get_financial_fact_pack",
                       "arguments": {"ticker": "MSFT", "as_of": "2026-09-16", "cik": "789019"}},
        })
        assert response.status_code == 200
        result = response.json()["result"]
        assert not result.get("isError"), result
        payload = result.get("structuredContent") or json.loads(result["content"][0]["text"])
        assert payload["facts"]["revenue"]["value"] == 331839000000
        assert len(calls) == 2
        failure = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "get_financial_fact_pack",
                       "arguments": {"ticker": "../SEC", "as_of": "2026-09-16"}},
        }).json()["result"]
        assert failure["isError"] is True
