"""Streamable HTTP MCP endpoint (JSON-RPC) at POST / and POST /mcp."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

router = APIRouter()

PROTOCOL = "2025-03-26"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "verify_person",
        "description": (
            "Find a work email from first name, last name, and company domain. "
            "Generates candidate addresses, verifies them, and returns status."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "first": {"type": "string", "description": "First name"},
                "last": {"type": "string", "description": "Last name"},
                "domain": {"type": "string", "description": "Company domain or website"},
            },
            "required": ["first", "last", "domain"],
        },
    },
    {
        "name": "start_run",
        "description": "Start a bulk name-to-email run from a list of people objects.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "people": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Rows with first/last/name and domain/website",
                },
                "max_cost": {"type": "number", "description": "USD ceiling for the run"},
            },
            "required": ["people"],
        },
    },
    {
        "name": "get_run",
        "description": "Get status, counts, and spend for a bulk run.",
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
        },
    },
    {
        "name": "export_run",
        "description": "Export a run segment as CSV text: valid, catchall, or unresolved.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "segment": {
                    "type": "string",
                    "enum": ["valid", "catchall", "unresolved"],
                },
            },
            "required": ["run_id", "segment"],
        },
    },
]


def _rpc_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


async def _call_tool(name: str, arguments: dict[str, Any]) -> Any:
    arguments = arguments or {}
    if name == "verify_person":
        from finder.api import VerifyRequest, verify_one

        return await verify_one(
            VerifyRequest(
                first=str(arguments.get("first") or ""),
                last=str(arguments.get("last") or ""),
                domain=str(arguments.get("domain") or ""),
            )
        )
    if name == "start_run":
        from finder.api import RunRequest, start_run

        return await start_run(
            RunRequest(
                people=list(arguments.get("people") or []),
                max_cost=arguments.get("max_cost"),
            )
        )
    if name == "get_run":
        from finder.api import get_run

        return await get_run(uuid.UUID(str(arguments["run_id"])))
    if name == "export_run":
        from finder.api import export_run

        response = await export_run(uuid.UUID(str(arguments["run_id"])), str(arguments.get("segment") or "valid"))
        return response.body.decode("utf-8") if isinstance(response.body, (bytes, bytearray)) else str(response.body)
    raise ValueError(f"unknown tool {name}")


async def _handle_message(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    req_id = message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        return _rpc_result(
            req_id,
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "name-to-email", "version": "0.1.0"},
                "instructions": (
                    "Name-to-email finder. Use verify_person for a single lookup, "
                    "start_run for a bulk CSV-like list. Catch-all is never mixed into valid."
                ),
            },
        )
    if method == "notifications/initialized" or (isinstance(method, str) and method.startswith("notifications/")):
        return None
    if method == "ping":
        return _rpc_result(req_id, {})
    if method == "tools/list":
        return _rpc_result(req_id, {"tools": TOOLS})
    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        try:
            result = await _call_tool(name, arguments)
            text = result if isinstance(result, str) else json.dumps(result, default=str)
            return _rpc_result(
                req_id,
                {"content": [{"type": "text", "text": text}], "isError": False},
            )
        except Exception as exc:
            logger.exception("mcp tool %s failed", name)
            return _rpc_result(
                req_id,
                {"content": [{"type": "text", "text": str(exc)}], "isError": True},
            )
    if req_id is None:
        return None
    return _rpc_error(req_id, -32601, f"Method not found: {method}")


def _mcp_headers(request: Request) -> dict[str, str]:
    session = request.headers.get("mcp-session-id") or str(uuid.uuid4())
    return {
        "mcp-session-id": session,
        "mcp-protocol-version": PROTOCOL,
    }


@router.api_route("/", methods=["POST"])
@router.api_route("/mcp", methods=["POST", "GET", "DELETE"])
async def mcp_endpoint(request: Request) -> Response:
    if request.method == "DELETE":
        return Response(status_code=204, headers=_mcp_headers(request))
    if request.method == "GET":
        # Empty SSE stream; clients that only POST still work.
        headers = _mcp_headers(request)
        headers["content-type"] = "text/event-stream"
        headers["cache-control"] = "no-cache"
        return Response(content="", status_code=200, headers=headers, media_type="text/event-stream")

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, status_code=400)

    messages = payload if isinstance(payload, list) else [payload]
    replies: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        reply = await _handle_message(message)
        if reply is not None:
            replies.append(reply)

    headers = _mcp_headers(request)
    if not replies:
        return Response(status_code=202, headers=headers)
    body = replies if isinstance(payload, list) else replies[0]
    accept = request.headers.get("accept", "")
    if "text/event-stream" in accept and "application/json" not in accept:
        data = json.dumps(body)
        headers["content-type"] = "text/event-stream"
        return Response(content=f"event: message\ndata: {data}\n\n", headers=headers, media_type="text/event-stream")
    return JSONResponse(body, headers=headers)
