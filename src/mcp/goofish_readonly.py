"""Local stdio MCP adapter for the read-only Goofish service API.

Run this process on the Hermes host, not on the NAS. It reads the service
address and bearer token from its environment and exposes only explicit,
read-only tools over JSON-RPC stdin/stdout.
"""

import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_SAFE_RESULT_FILENAME = re.compile(r"^[^/\\]+\.jsonl$")


@dataclass
class GoofishReadOnlyClient:
    base_url: str
    token: str
    fetch_json: Optional[Callable[[str, dict[str, str]], object]] = None

    def __post_init__(self):
        self.base_url = self.base_url.rstrip("/")
        if not self.base_url.startswith("https://"):
            raise ValueError("GOOFISH_MCP_BASE_URL must use https")
        if not self.token:
            raise ValueError("GOOFISH_MCP_READ_TOKEN is required")
        if self.fetch_json is None:
            self.fetch_json = self._fetch_json

    def _fetch_json(self, path: str, headers: dict[str, str]):
        request = Request(self.base_url + path, headers=headers)
        with urlopen(request, timeout=20) as response:
            return json.load(response)

    def _get_api(self, path: str):
        assert self.fetch_json is not None
        return self.fetch_json(path, {"Authorization": "Bearer " + self.token})

    def _get_public(self, path: str):
        assert self.fetch_json is not None
        return self.fetch_json(path, {})

    def health(self):
        return self._get_public("/health")

    def list_tasks(self):
        return self._get_api("/api/tasks")

    def task(self, task_id: int):
        return self._get_api("/api/tasks/" + str(_positive_int(task_id, "task_id")))

    def dashboard_summary(self):
        return self._get_api("/api/dashboard/summary")

    def result_files(self):
        return self._get_api("/api/results/files")

    def result_records(self, filename: str, page: int = 1, limit: int = 50):
        if not isinstance(filename, str) or not _SAFE_RESULT_FILENAME.fullmatch(filename):
            raise ValueError("filename must be a single .jsonl filename")
        params = urlencode({"page": _positive_int(page, "page"), "limit": _bounded_limit(limit)})
        return self._get_api("/api/results/" + filename + "?" + params)

    def task_logs(self, task_id: int, offset_lines: int = 0, limit_lines: int = 50):
        task_id = _positive_int(task_id, "task_id")
        if not isinstance(offset_lines, int) or offset_lines < 0:
            raise ValueError("offset_lines must be a non-negative integer")
        params = urlencode({"task_id": task_id, "offset_lines": offset_lines, "limit_lines": _bounded_limit(limit_lines)})
        return self._get_api("/api/logs/tail?" + params)


def _positive_int(value, name: str) -> int:
    if not isinstance(value, int) or value < 0:
        raise ValueError(name + " must be a non-negative integer")
    return value


def _bounded_limit(value) -> int:
    if not isinstance(value, int) or not 1 <= value <= 100:
        raise ValueError("limit must be an integer from 1 to 100")
    return value


TOOLS = [
    {"name": "goofish_health", "description": "Read the unauthenticated service health result.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "goofish_list_tasks", "description": "List monitor tasks. Read-only.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "goofish_get_task", "description": "Read one monitor task. Read-only.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "integer", "minimum": 0}}, "required": ["task_id"]}},
    {"name": "goofish_dashboard_summary", "description": "Read dashboard summary. Read-only.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "goofish_list_result_files", "description": "List result files. Read-only.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "goofish_get_result_records", "description": "Read paginated records from a result file. Read-only.", "inputSchema": {"type": "object", "properties": {"filename": {"type": "string"}, "page": {"type": "integer", "minimum": 1}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, "required": ["filename"]}},
    {"name": "goofish_get_task_logs", "description": "Read tail logs for one task. Read-only.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "integer", "minimum": 0}, "offset_lines": {"type": "integer", "minimum": 0}, "limit_lines": {"type": "integer", "minimum": 1, "maximum": 100}}, "required": ["task_id"]}},
]


def execute_tool(client: GoofishReadOnlyClient, name: str, arguments: dict):
    dispatch = {
        "goofish_health": lambda: client.health(),
        "goofish_list_tasks": lambda: client.list_tasks(),
        "goofish_get_task": lambda: client.task(arguments["task_id"]),
        "goofish_dashboard_summary": lambda: client.dashboard_summary(),
        "goofish_list_result_files": lambda: client.result_files(),
        "goofish_get_result_records": lambda: client.result_records(arguments["filename"], arguments.get("page", 1), arguments.get("limit", 50)),
        "goofish_get_task_logs": lambda: client.task_logs(arguments["task_id"], arguments.get("offset_lines", 0), arguments.get("limit_lines", 50)),
    }
    if name not in dispatch:
        raise ValueError("unknown read-only tool")
    return dispatch[name]()


def _reply(request_id, result=None, error=None):
    payload = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = {"code": -32602, "message": error}
    else:
        payload["result"] = result
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def main():
    client = GoofishReadOnlyClient(
        os.environ.get("GOOFISH_MCP_BASE_URL", ""),
        os.environ.get("GOOFISH_MCP_READ_TOKEN", ""),
    )
    for line in sys.stdin:
        try:
            request = json.loads(line)
            method = request.get("method")
            request_id = request.get("id")
            if method == "initialize":
                _reply(request_id, {"protocolVersion": "2025-06-18", "serverInfo": {"name": "goofish-readonly", "version": "0.1.0"}, "capabilities": {"tools": {}}})
            elif method == "tools/list":
                _reply(request_id, {"tools": TOOLS})
            elif method == "tools/call":
                params = request.get("params") or {}
                value = execute_tool(client, params.get("name", ""), params.get("arguments") or {})
                _reply(request_id, {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]})
            elif request_id is not None:
                _reply(request_id, error="unsupported method")
        except Exception as exc:
            if "request_id" in locals() and request_id is not None:
                _reply(request_id, error=str(exc))


if __name__ == "__main__":
    main()
