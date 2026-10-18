from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

logger = logging.getLogger("porchvoice-mcp")


def bearer_headers(token: str) -> dict:
    token = (token or "").strip()
    if not token:
        return {}
    return {"Authorization": "Bearer " + token}


def create_mcp_server(url: str, token: str = "", timeout: float = 5.0):
    from livekit.agents import mcp

    return mcp.MCPServerHTTP(
        url=url,
        headers=bearer_headers(token) or None,
        timeout=timeout,
        client_session_timeout_seconds=timeout,
    )


def create_mcp_toolset(url: str, token: str = "", timeout: float = 5.0):
    from livekit.agents import mcp

    server = create_mcp_server(url, token, timeout)
    return mcp.MCPToolset(id="porchvoice-mcp", mcp_server=server)


def _is_streamable(url: str) -> bool:
    path = urlparse(url).path.lower().rstrip("/")
    return path.endswith("/mcp")


async def _with_session(url: str, token: str, action):
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import (
        create_mcp_http_client,
        streamable_http_client,
    )

    headers = bearer_headers(token)

    async def _sse():
        async with sse_client(url, headers=headers or None) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                return await action(session)

    async def _http():
        http_client = create_mcp_http_client(headers=headers or None)
        async with streamable_http_client(url, http_client=http_client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                return await action(session)

    return await (_http() if _is_streamable(url) else _sse())


async def probe_mcp(url: str, token: str = "", timeout: float = 5.0) -> dict:
    async def _list(session) -> dict:
        tools = await session.list_tools()
        return {"ok": True, "tools": [t.name for t in tools.tools], "error": ""}

    try:
        return await asyncio.wait_for(_with_session(url, token, _list), timeout=timeout + 5)
    except Exception as exc:
        return {"ok": False, "tools": [], "error": f"{type(exc).__name__}: {exc}"[:300]}


async def call_mcp_tool(url: str, tool_name: str, arguments: dict,
                        token: str = "", timeout: float = 5.0) -> dict:
    async def _call(session) -> dict:
        result = await session.call_tool(tool_name, arguments or {})
        texts = []
        for item in getattr(result, "content", None) or []:
            text = getattr(item, "text", None)
            if text is not None:
                texts.append(str(text))
        return {"ok": True, "texts": texts, "isError": bool(getattr(result, "isError", False))}

    try:
        return await asyncio.wait_for(
            _with_session(url, token, _call), timeout=timeout + 15)
    except Exception as exc:
        return {"ok": False, "texts": [], "error": f"{type(exc).__name__}: {exc}"[:300]}
