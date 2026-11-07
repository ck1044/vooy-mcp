"""Check MCP discovery and a tool call using the local token file."""

import asyncio
import os
from pathlib import Path

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main():
    root = Path(__file__).resolve().parents[1]
    token_path = Path(os.environ.get("VOOY_MCP_TOKEN_FILE", str(root / ".secrets" / "mcp-token")))
    token = token_path.read_text(encoding="utf-8").strip()
    url = os.environ.get("VOOY_MCP_URL", "http://127.0.0.1:3185/mcp")
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, trust_env=False
    ) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                info = await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool("ping", {"message": "안녕하세요"})
                if result.is_error:
                    raise RuntimeError("ping 도구 호출 실패")
                print(f"연결 성공 (MCP {info.protocol_version})")
                print("도구:", ", ".join(tool.name for tool in tools.tools))
                print("응답:", result.content[0].text)


if __name__ == "__main__":
    asyncio.run(main())
