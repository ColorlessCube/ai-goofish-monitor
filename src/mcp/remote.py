"""Stateless Streamable HTTP MCP using JSON responses and scoped tools.

No SSE subscriptions or transport sessions are advertised. Business operations
are service-local; a browser administrator cookie never grants MCP authority.
"""
import asyncio
import importlib
import json

from src.mcp import journal
from src.mcp.outcomes import MutationOutcomeError
from src.mcp.redaction import safe_payload
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.mcp.authentication import authenticate

router = APIRouter()
MAX_REQUEST_BYTES = 131072
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26")


class ReceiptQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


async def read_receipt(args: ReceiptQuery, ctx):
    receipt = journal.get_receipt(ctx["principal"]["subject"], args.request_id)
    if receipt is None:
        raise HTTPException(404, "invocation_not_found")
    stored = receipt.pop("result")
    # Journal-generated lookup metadata is bounded separately from the one
    # canonical business payload, not the two copies inside CallToolResult.
    receipt, _ = safe_payload(receipt)
    trusted = {key: receipt[key] for key in ("invocation_id", "request_id", "status", "retry_allowed")}
    receipt["result"] = None if stored is None else tool_result(
        stored["structuredContent"], failed=stored["isError"], receipt=trusted,
    )
    return receipt


def receipt_tools():
    return [{"name": "get_invocation", "description": "Read your own durable write receipt. Unknown does not mean not executed; do not retry with a new ID.", "input_model": ReceiptQuery, "scope": "system:read", "read_only": True, "destructive": False, "handler": read_receipt}]


def get_tools() -> list[dict]:
    tools = receipt_tools()
    for module in ("task_tools", "prompt_tools", "result_tools", "diagnostic_tools", "admin_tools"):
        tools.extend(importlib.import_module("src.mcp." + module).build_tools())
    names = [t["name"] for t in tools]
    if len(names) != len(set(names)):
        raise RuntimeError("duplicate_mcp_tool")
    return tools


def error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def result(request_id, payload) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _render_tool_result(structured: dict, *, failed=False) -> dict:
    """Render already scrubbed/bounded data with identical text and structure."""
    return {"content": [{"type": "text", "text": json.dumps(structured, ensure_ascii=False, allow_nan=False)}], "structuredContent": structured, "isError": failed}


def tool_result(payload, *, failed=False, receipt=None) -> dict:
    structured = dict(payload) if isinstance(payload, dict) else {"items": payload}
    if receipt is not None:
        # This argument is transport-owned metadata, never a handler-provided
        # _receipt. Budget it with the payload, then retain it on truncation.
        structured["_receipt"] = receipt
    structured, too_large = safe_payload(structured)
    if too_large and receipt is not None:
        structured["_receipt"] = receipt
    # Truncating output is not a change to the mutation's execution outcome.
    failed = failed or (too_large and receipt is None)
    return _render_tool_result(structured, failed=failed)


def tool_schema(tool: dict) -> dict:
    schema = tool["input_model"].model_json_schema()
    if not tool["read_only"]:
        schema.setdefault("properties", {})["request_id"] = {"type": "string", "format": "uuid", "description": "Stable UUID for this operation. Reuse unchanged after timeout; inspect its receipt before retrying."}
        schema.setdefault("required", []).append("request_id")
    return {"name": tool["name"], "description": tool["description"], "inputSchema": schema,
            "annotations": {"readOnlyHint": tool["read_only"], "destructiveHint": tool["destructive"], "idempotentHint": True, "openWorldHint": not tool["read_only"]}}


def unique_pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate_json_key")
        obj[key] = value
    return obj


async def execute_mutation(tool, validated, arguments, request_id, ctx):
    with journal.mutation_lock() as acquired:
        if not acquired:
            return tool_result({"error": "write_busy", "retry": "Retry unchanged with the same request_id; never choose a new ID."}, failed=True)
        action, row = journal.claim(ctx["principal"]["subject"], request_id, tool["name"], arguments)
        if action == "conflict":
            return tool_result({"error": "request_id_payload_conflict"}, failed=True)
        if action == "existing":
            if row["response_json"]:
                stored = json.loads(row["response_json"])
                # Rebuild text from scrubbed structured data, retaining the
                # original execution outcome. Never rewrite the historical row.
                receipt = {"invocation_id": row["invocation_id"], "request_id": request_id, "status": row["status"], "retry_allowed": False}
                return tool_result(stored["structuredContent"], failed=stored["isError"], receipt=receipt)
            return tool_result({"error": "outcome_unknown", "_receipt": {"invocation_id": row["invocation_id"], "request_id": request_id, "status": "running_or_unknown", "retry_allowed": False}}, failed=True)
        status, failed = "succeeded", False
        payload: object
        try:
            async with asyncio.timeout(90):
                payload = await tool["handler"](validated, ctx)
        except MutationOutcomeError as exc:
            status, failed = exc.status, True
            payload = exc.payload
        except HTTPException as exc:
            status, failed = "unknown" if exc.status_code >= 500 else "failed", True
            payload = {"error": "operation_rejected", "status": exc.status_code}
        except (Exception, asyncio.CancelledError):
            status, failed = "unknown", True
            payload = {"error": "operation_outcome_unknown", "retry_allowed": False}
        receipt = {"invocation_id": row["invocation_id"], "request_id": request_id, "status": status, "retry_allowed": False}
        response = tool_result(payload, failed=failed, receipt=receipt)
        journal.finish(row["invocation_id"], status, response)
        return response


async def dispatch(body: dict, request: Request, principal: dict):
    rid, method, params = body["id"], body["method"], body.get("params", {})
    if method == "initialize":
        version = params.get("protocolVersion")
        return result(rid, {"protocolVersion": version if version in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0], "serverInfo": {"name": "goofish", "version": "1.0.0"}, "capabilities": {"tools": {"listChanged": False}}})
    if method == "ping":
        return result(rid, {})
    if method not in {"tools/list", "tools/call"}:
        return error(rid, -32601, "method_not_found")
    tools = get_tools()
    if method == "tools/list":
        if params.get("cursor") is not None:
            return error(rid, -32602, "invalid_cursor")
        return result(rid, {"tools": [tool_schema(t) for t in tools if t["scope"] in principal["scopes"]]})
    tool = next((t for t in tools if t["name"] == params.get("name")), None)
    if tool is None or tool["scope"] not in principal["scopes"]:
        return error(rid, -32602, "tool_unavailable_or_not_authorized")
    args = params.get("arguments", {})
    if not isinstance(args, dict):
        return error(rid, -32602, "arguments_must_be_object")
    args = dict(args)
    request_id = None
    if not tool["read_only"]:
        request_id = args.pop("request_id", None)
        try:
            if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
                raise ValueError("invalid_request_id")
        except ValueError:
            return error(rid, -32602, "canonical_request_id_uuid_required")
    try:
        validated = tool["input_model"].model_validate(args)
    except ValidationError:
        return error(rid, -32602, "invalid_tool_arguments")
    ctx = {"app": request.app, "principal": principal}
    if not tool["read_only"]:
        return result(rid, await execute_mutation(tool, validated, args, request_id, ctx))
    try:
        async with asyncio.timeout(30):
            payload = await tool["handler"](validated, ctx)
        if tool["handler"] is read_receipt:
            # Only the trusted journal reader returns separately bounded parts.
            # Ordinary handler dictionaries cannot opt out of output limits.
            return result(rid, _render_tool_result(payload))
        return result(rid, tool_result(payload))
    except HTTPException as exc:
        return result(rid, tool_result({"error": "operation_rejected", "status": exc.status_code}, failed=True))
    except Exception:
        return result(rid, tool_result({"error": "operation_failed"}, failed=True))


@router.api_route("/api/mcp", methods=["GET", "DELETE", "OPTIONS"])
async def unsupported_transport(request: Request):
    authenticate(request)
    return Response(status_code=405, headers={"Allow": "POST"})


@router.post("/api/mcp")
async def mcp_endpoint(request: Request):
    principal = authenticate(request)
    accept = {part.strip().split(";")[0] for part in request.headers.get("accept", "").split(",")}
    if not {"application/json", "text/event-stream"}.issubset(accept):
        raise HTTPException(406, "mcp_json_accept_required")
    if request.headers.get("content-type", "").split(";")[0].lower() != "application/json":
        raise HTTPException(415, "mcp_json_required")
    version = request.headers.get("mcp-protocol-version")
    if version is not None and version not in SUPPORTED_VERSIONS:
        raise HTTPException(400, "mcp_protocol_version_unsupported")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_REQUEST_BYTES:
            raise HTTPException(413, "mcp_request_too_large")
    try:
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError):
        return JSONResponse(error(None, -32700, "invalid_json"), status_code=400)
    valid = isinstance(body, dict) and body.get("jsonrpc") == "2.0" and isinstance(body.get("method"), str) and isinstance(body.get("params", {}), dict)
    if not valid:
        return JSONResponse(error(None, -32600, "invalid_request"), status_code=400)
    try:
        # JSON escapes can decode to lone surrogates even when the original
        # bytes are UTF-8. Validate all strings before dispatch or echoing IDs.
        json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, RecursionError):
        return JSONResponse(error(None, -32600, "invalid_request"), status_code=400)
    if "id" not in body and body["method"].startswith("notifications/"):
        return Response(status_code=202)
    if type(body.get("id")) not in {str, int}:
        return JSONResponse(error(None, -32600, "request_id_required"), status_code=400)
    rid = body["id"]
    try:
        response = await dispatch(body, request, principal)
        return JSONResponse(response, headers={"Cache-Control": "no-store"})
    except Exception:
        # Rendering belongs inside the error boundary too. Only the already
        # validated request ID is retained in this fixed, serializable fallback.
        return JSONResponse(error(rid, -32603, "server_error"), headers={"Cache-Control": "no-store"})
