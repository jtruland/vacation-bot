"""
Lightweight MCP (Model Context Protocol) client for mcp.bitz.dev.
Transport: HTTP + SSE (MCP protocol 2024-11-05).
Auth: API key via Authorization: Bearer header.
"""
import json
import logging
import os
import queue
import threading

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

logger = logging.getLogger(__name__)

_BASE_URL = "https://mcp.bitz.dev"


def _get_api_key() -> str:
    key = os.getenv("MCP_API_KEY")
    if not key:
        raise RuntimeError("MCP_API_KEY not set in .env — ask Jon to add it.")
    return key


def call_tool(tool_name: str, arguments: dict) -> str:
    """
    Call a tool on the MCP gateway and return its text output.

    Opens an SSE connection, sends the MCP initialize handshake, calls the
    tool, waits for the result event, then closes the connection.
    Each call is independent (no persistent session).
    """
    api_key = _get_api_key()
    auth_headers = {"Authorization": f"Bearer {api_key}"}

    result_queue: queue.Queue = queue.Queue()
    post_url_holder: list[str | None] = [None]
    endpoint_ready = threading.Event()
    sse_error: list[Exception | None] = [None]

    def _sse_reader():
        try:
            with requests.get(
                f"{_BASE_URL}/sse",
                headers={**auth_headers, "Accept": "text/event-stream"},
                stream=True,
                timeout=60,
            ) as resp:
                resp.raise_for_status()
                event_type: str | None = None
                for raw_line in resp.iter_lines():
                    if not raw_line:
                        event_type = None
                        continue
                    line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line

                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                        continue

                    if line.startswith("data:"):
                        data = line[5:].strip()
                        if event_type == "endpoint":
                            url = data if data.startswith("http") else _BASE_URL + data
                            post_url_holder[0] = url
                            endpoint_ready.set()
                        elif event_type in ("message", None) and post_url_holder[0]:
                            try:
                                msg = json.loads(data)
                                if isinstance(msg, dict) and msg.get("id") == 2:
                                    result_queue.put(("ok", msg))
                                    return
                            except (json.JSONDecodeError, Exception):
                                pass
        except Exception as exc:
            sse_error[0] = exc
            endpoint_ready.set()
            result_queue.put(("error", exc))

    thread = threading.Thread(target=_sse_reader, daemon=True)
    thread.start()

    if not endpoint_ready.wait(timeout=15):
        raise RuntimeError(f"MCP gateway timeout: no endpoint event within 15s for {tool_name}")

    if sse_error[0]:
        raise RuntimeError(f"MCP SSE connection failed: {sse_error[0]}")

    post_url = post_url_holder[0]
    post_headers = {**auth_headers, "Content-Type": "application/json"}

    # MCP handshake: initialize
    requests.post(post_url, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "vacation-bot", "version": "1.0"},
        },
    }, headers=post_headers, timeout=10).raise_for_status()

    # MCP handshake: initialized notification
    requests.post(post_url, json={
        "jsonrpc": "2.0", "method": "notifications/initialized",
    }, headers=post_headers, timeout=10)

    # Tool call
    requests.post(post_url, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }, headers=post_headers, timeout=10).raise_for_status()

    # Wait for result on SSE stream
    try:
        status, payload = result_queue.get(timeout=30)
    except queue.Empty:
        raise RuntimeError(f"MCP tool '{tool_name}' timed out waiting for result (30s)")

    if status == "error":
        raise RuntimeError(f"MCP call failed: {payload}")

    if "error" in payload:
        err = payload["error"]
        raise RuntimeError(f"MCP tool error ({tool_name}): {err.get('message', err)}")

    content = payload.get("result", {}).get("content", [])
    return "\n".join(item["text"] for item in content if item.get("type") == "text")
