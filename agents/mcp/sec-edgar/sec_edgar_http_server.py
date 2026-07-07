"""Streamable-HTTP host for the SEC EDGAR MCP server, gated by a shared-secret header.

This wraps the open-source ``sec-edgar-mcp`` package (a FastMCP server over the SEC
EDGAR REST data API) and serves it over the MCP streamable-HTTP transport so it can be
registered as a **governed Foundry toolbox MCP tool** (``MCPToolboxTool`` with
``server_url`` + ``headers``), instead of being imported in-process inside each hosted
agent container.

``sec-edgar-mcp`` ships no authentication, so we mount its ASGI app behind a tiny
Starlette middleware that requires a shared-secret header on every MCP request. The
Foundry toolbox tool sends that header (configured via ``headers`` on the tool), which
closes the "unauthenticated public transport" gap that previously kept the tool private
in-container.

Endpoints:
  GET  /healthz   -> unauthenticated liveness probe ("ok")
  POST /mcp       -> MCP streamable-HTTP transport (requires the auth header)

Environment:
  SEC_EDGAR_USER_AGENT   required contact string for SEC EDGAR (e.g. "Name (email)")
  FSI_MCP_KEY            shared secret required in the auth header (auth disabled if unset)
  FSI_MCP_KEY_HEADER     header name carrying the secret (default "x-fsi-mcp-key")
  MCP_PATH              streamable-HTTP mount path (default "/mcp")
  PORT                  listen port (default 8080)
"""
from __future__ import annotations

import hmac
import logging
import os
import sys

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("secedgar.http")

API_KEY = os.environ.get("FSI_MCP_KEY", "")
KEY_HEADER = os.environ.get("FSI_MCP_KEY_HEADER", "x-fsi-mcp-key").lower()
MCP_PATH = os.environ.get("MCP_PATH", "/mcp")
HEALTH_PATHS = {"/healthz", "/"}


class ApiKeyAuthMiddleware(BaseHTTPMiddleware):
    """Require a constant shared-secret header on every non-health request."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path in HEALTH_PATHS:
            return await call_next(request)
        if not API_KEY:
            # Auth intentionally disabled (local dev only); log loudly.
            logger.warning("FSI_MCP_KEY is unset - serving MCP without authentication")
            return await call_next(request)
        provided = request.headers.get(KEY_HEADER, "")
        if not (provided and hmac.compare_digest(provided, API_KEY)):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


async def _health(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def build_app():
    if not os.environ.get("SEC_EDGAR_USER_AGENT"):
        logger.error("SEC_EDGAR_USER_AGENT is required by sec-edgar-mcp; refusing to start.")
        sys.exit(1)

    # Imported lazily: sec_edgar_mcp.server instantiates SEC tool clients at import
    # time, which need SEC_EDGAR_USER_AGENT to be present first.
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings
    from sec_edgar_mcp.server import register_tools

    # The MCP SDK's DNS-rebinding protection validates the inbound Host/Origin header
    # against an allow-list and returns 421 Misdirected Request for anything else. Behind
    # Container Apps ingress the public FQDN never matches the default allow-list, so we
    # turn the protection off — every request is already gated by our shared-secret
    # header (ApiKeyAuthMiddleware), which is the real access control here.
    transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

    # stateless_http + json_response keep each request self-contained (no session
    # affinity), which is robust behind Container Apps ingress and matches how the
    # Foundry remote-MCP client calls the server.
    mcp = FastMCP(
        "SEC EDGAR MCP",
        stateless_http=True,
        json_response=True,
        streamable_http_path=MCP_PATH,
        transport_security=transport_security,
    )
    register_tools(mcp)

    app = mcp.streamable_http_app()
    app.router.routes.append(Route("/healthz", _health, methods=["GET"]))
    # add_middleware wraps outermost, so it runs before the MCP transport; /healthz is
    # exempted inside the middleware.
    app.add_middleware(ApiKeyAuthMiddleware)
    logger.info("SEC EDGAR MCP streamable-HTTP app ready at %s (auth=%s)", MCP_PATH, bool(API_KEY))
    return app


app = build_app()


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
