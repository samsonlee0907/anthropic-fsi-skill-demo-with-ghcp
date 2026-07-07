import os
import anyio
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession


async def main():
    url = os.environ["MCP_URL"]
    headers = {"x-fsi-mcp-key": os.environ["FSI_MCP_KEY"]}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print("TOOL_COUNT", len(names))
            print("TOOLS", names)
            res = await session.call_tool("get_company_info", {"identifier": "AAPL"})
            text = "".join(getattr(c, "text", "") for c in res.content)
            print("COMPANY_INFO", text[:300])
            res2 = await session.call_tool(
                "get_recent_filings",
                {"identifier": "AAPL", "form_type": "10-K", "days": 730, "limit": 1},
            )
            text2 = "".join(getattr(c, "text", "") for c in res2.content)
            print("RECENT_FILINGS", text2[:400])


anyio.run(main)
