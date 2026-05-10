"""
Lightweight MCP (Model Context Protocol) client for mcp.bitz.dev.
Transport: Streamable HTTP (MCP protocol 2024-11-05, FastMCP 3.x).
Auth: API key via Authorization: Bearer header.

Session flow:
  - POST /mcp (initialize, stream=True) — keeps connection open to hold session alive
  - GET session ID from response headers
  - POST /mcp (notifications/initialized) with session ID
  - POST /mcp (tools/call) with session ID — result returned as SSE in POST response
  - Init stream closed after tool result is received
"""
import json
import logging
import os
import threading

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

    Opens a streaming initialize POST to establish a session, makes the
    tool call on a separate POST (response arrives as SSE in that POST),
    then closes the init stream.
    """
    api_key = _get_api_key()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    session_id_holder: list[str | None] = [None]
    session_ready = threading.Event()
    session_done = threading.Event()
    init_error: list[Exception | None] = [None]

    def _init_stream() -> None:
        try:
            with requests.post(
                _MCP_URL,
                json={
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "vacation-bot", "version": "1.0"},
                    },
                },
                headers=headers,
                timeout=120,
                stream=True,
            ) as resp:
                resp.raise_for_status()
                session_id_holder[0] = resp.headers.get("mcp-session-id")
                session_ready.set()
                session_done.wait(timeout=90)
        except Exception as exc:
            init_error[0] = exc
            session_ready.set()

    t = threading.Thread(target=_init_stream, daemon=True)
    t.start()

    if not session_ready.wait(timeout=15):
        raise RuntimeError(f"MCP gateway timeout: no session within 15s for {tool_name}")

    if init_error[0]:
        raise RuntimeError(f"MCP init failed: {init_error[0]}")

    try:
        session_id = session_id_holder[0]
        post_headers = {**headers, "mcp-session-id": session_id}

        # Initialized notification (202, no body needed)
        requests.post(_MCP_URL, json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }, headers=post_headers, timeout=10)

        # Tool call — response is SSE stream in the POST response body
        resp = requests.post(_MCP_URL, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }, headers=post_headers, timeout=60, stream=True)
        resp.raise_for_status()

        result = _parse_sse_result(resp, tool_name)
    finally:
        session_done.set()

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
