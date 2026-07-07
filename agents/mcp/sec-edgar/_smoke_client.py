import anyio
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession


async def main():
    headers = {"x-fsi-mcp-key": "local-test-secret"}
    async with streamablehttp_client("http://localhost:8080/mcp", headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print("TOOL_COUNT", len(names))
            print("TOOLS", names)
            res = await session.call_tool("get_company_info", {"identifier": "AAPL"})
            text = ""
            for c in res.content:
                text += getattr(c, "text", "")
            print("COMPANY_INFO", text[:400])


anyio.run(main)
