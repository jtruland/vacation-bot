"""
Lightweight MCP (Model Context Protocol) client for mcp.bitz.dev.
Transport: Streamable HTTP (MCP protocol 2024-11-05, FastMCP 3.x).
Auth: API key via Authorization: Bearer header.
"""
import json
import logging
import os

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

logger = logging.getLogger(__name__)

_BASE_URL = "https://mcp.bitz.dev"
_MCP_URL = f"{_BASE_URL}/mcp"


def _get_api_key() -> str:
    key = os.getenv("MCP_API_KEY")
    if not key:
        raise RuntimeError("MCP_API_KEY not set in .env — ask Jon to add it.")
    return key


def call_tool(tool_name: str, arguments: dict) -> str:
    """
    Call a tool on the MCP gateway and return its text output.

    Uses streamable HTTP transport: POST to /mcp for every step.
    Each call is independent (no persistent session).
    """
    api_key = _get_api_key()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    # Initialize — get session ID from response header
    resp = requests.post(_MCP_URL, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "vacation-bot", "version": "1.0"},
        },
    }, headers=headers, timeout=15)
    resp.raise_for_status()

    session_id = resp.headers.get("mcp-session-id")
    if session_id:
        headers["mcp-session-id"] = session_id

    # Initialized notification
    requests.post(_MCP_URL, json={
        "jsonrpc": "2.0", "method": "notifications/initialized",
    }, headers=headers, timeout=10)

    # Tool call — may return JSON or SSE stream
    resp = requests.post(_MCP_URL, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }, headers=headers, timeout=60, stream=True)
    resp.raise_for_status()

    content_type = resp.headers.get("Content-Type", "")
    if "text/event-stream" in content_type:
        result = _parse_sse_result(resp, tool_name)
    else:
        result = resp.json()

    if "error" in result:
        err = result["error"]
        raise RuntimeError(f"MCP tool error ({tool_name}): {err.get('message', err)}")

    content = result.get("result", {}).get("content", [])
    from shared.api_usage import record_mcp
    record_mcp()
    return "\n".join(item["text"] for item in content if item.get("type") == "text")


def _parse_sse_result(resp: requests.Response, tool_name: str) -> dict:
    """Parse an SSE response stream and return the JSON-RPC result with id=2."""
    for raw_line in resp.iter_lines():
        if not raw_line:
            continue
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        try:
            msg = json.loads(data)
            if isinstance(msg, dict) and msg.get("id") == 2:
                return msg
        except json.JSONDecodeError:
            pass
    raise RuntimeError(f"MCP tool '{tool_name}': no result in SSE stream")
