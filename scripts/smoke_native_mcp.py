"""Exercise native MCP only against an empty disposable loopback service."""
import argparse
import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from uuid import uuid4


class SmokeFailure(RuntimeError):
    pass


def validate_origin(value: str) -> str:
    try:
        url = urlsplit(value)
        valid = (url.scheme == "http" and url.hostname in {"127.0.0.1", "::1"}
                 and url.port is not None and not url.username and not url.password
                 and url.path in {"", "/"} and not url.query and not url.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise SmokeFailure("disposable_numeric_loopback_origin_required")
    return value.rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SmokeFailure("redirect_denied")


def require(condition, code):
    if not condition:
        raise SmokeFailure(code)


class Client:
    def __init__(self, origin, token):
        self.origin = validate_origin(origin)
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.sequence = 0

    def rpc(self, method, params=None):
        self.sequence += 1
        body = {"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params or {}}
        request = urllib.request.Request(self.origin + "/api/mcp", data=json.dumps(body).encode(), headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"})
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(300001)
                require(len(raw) <= 300000, "response_too_large")
                value = json.loads(raw)
        except (urllib.error.URLError, OSError, ValueError):
            raise SmokeFailure("http_or_json_failure") from None
        require(value.get("jsonrpc") == "2.0" and value.get("id") == self.sequence, "rpc_response_mismatch")
        return value

    def call(self, name, args=None, expect_error=False):
        value = self.rpc("tools/call", {"name": name, "arguments": args or {}})
        failed = "error" in value or value.get("result", {}).get("isError") is True
        require(failed if expect_error else not failed, "tool_expectation_failed:" + name)
        if failed:
            return value
        structured = value["result"].get("structuredContent")
        require(isinstance(structured, dict), "structured_result_required")
        return structured


def run_smoke(base_url, read_token, write_token):
    require(len(read_token) >= 32 and len(write_token) >= 32 and read_token != write_token, "distinct_disposable_tokens_required")
    reader, writer = Client(base_url, read_token), Client(base_url, write_token)
    for client in (reader, writer):
        initialized = client.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "goofish-release-smoke", "version": "1"}})
        require(initialized.get("result", {}).get("serverInfo", {}).get("name") == "goofish", "initialization_failed")
    read_tools = reader.rpc("tools/list").get("result", {}).get("tools", [])
    write_tools = writer.rpc("tools/list").get("result", {}).get("tools", [])
    require("create_task" not in {t["name"] for t in read_tools}, "read_scope_exposed_mutation")
    expected = {"create_task", "get_task", "list_tasks", "update_task", "preview_task_deletion", "delete_task", "get_invocation"}
    require(expected.issubset({t["name"] for t in write_tools}), "crud_tool_catalog_incomplete")
    before = reader.call("list_tasks")
    require(before.get("total") == 0 and before.get("items") == [], "empty_disposable_service_required")
    name = "mcp-smoke-" + uuid4().hex
    request_id = str(uuid4())
    args = {"task_name": name, "keyword": name, "description": "Disposable MCP CRUD fixture; never run.", "decision_mode": "keyword", "keyword_rules": [name], "enabled": False, "cron": None, "request_id": request_id}
    reader.call("create_task", args, expect_error=True)
    created = writer.call("create_task", args)
    task = created.get("task", {})
    require(type(task.get("id")) is int and task.get("task_name") == name and task.get("enabled") is False and not task.get("is_running"), "created_task_readback_failed")
    task_id = task["id"]
    repeated = writer.call("create_task", args)
    require(repeated == created, "idempotency_receipt_changed")
    listed = reader.call("list_tasks")
    require(listed.get("total") == 1 and len(listed.get("items", [])) == 1, "create_replayed_or_list_incomplete")
    read = reader.call("get_task", {"task_id": task_id})
    revision = read.get("revision")
    require(isinstance(revision, str) and revision, "revision_missing")
    updated = writer.call("update_task", {"task_id": task_id, "expected_revision": revision, "changes": {"task_name": name + "-updated"}, "request_id": str(uuid4())})
    require(updated.get("task", {}).get("task_name") == name + "-updated", "update_readback_failed")
    writer.call("update_task", {"task_id": task_id, "expected_revision": revision, "changes": {"task_name": name + "-stale"}, "request_id": str(uuid4())}, expect_error=True)
    after = reader.call("get_task", {"task_id": task_id})
    require(after.get("task", {}).get("task_name") == name + "-updated", "stale_revision_changed_state")
    preview = writer.call("preview_task_deletion", {"task_id": task_id})
    delete_id = str(uuid4())
    deleted = writer.call("delete_task", {"task_id": task_id, "expected_revision": preview["revision"], "request_id": delete_id})
    require(deleted.get("_receipt", {}).get("status") == "succeeded", "delete_receipt_failed")
    receipt = writer.call("get_invocation", {"request_id": delete_id})
    require(receipt.get("status") == "succeeded", "durable_receipt_failed")
    reader.call("get_task", {"task_id": task_id}, expect_error=True)
    remaining = reader.call("list_tasks")
    require(remaining.get("total") == 0 and remaining.get("items") == [], "fixture_cleanup_readback_failed")
    return {"status": "passed", "read_tools": len(read_tools), "write_tools": len(write_tools), "remaining_tasks": remaining["total"], "write_replay_verified": True, "stale_revision_rejected": True, "crawler_started": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--read-token-env", default="GOOFISH_MCP_READ_TOKEN")
    parser.add_argument("--write-token-env", default="GOOFISH_MCP_WRITE_TOKEN")
    args = parser.parse_args()
    try:
        receipt = run_smoke(args.base_url, os.environ.get(args.read_token_env, ""), os.environ.get(args.write_token_env, ""))
    except SmokeFailure as exc:
        print(json.dumps({"status": "failed", "code": str(exc)}))
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
