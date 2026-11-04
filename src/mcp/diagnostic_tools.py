"""Read-only native diagnostics: local evidence, no credential values or active probes."""
from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path

from fastapi import HTTPException
from pydantic import Field

from src.infrastructure.persistence.sqlite_connection import get_database_path
from src.api import dependencies
from src.mcp.result_admin_helpers import Input, dependency, redact, redact_log_lines, tool


async def health(args, ctx):
    def check():
        try:
            path = Path(get_database_path()).resolve()
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
                conn.execute("SELECT 1 FROM tasks LIMIT 1").fetchall()
            return "available"
        except Exception:
            return "unknown"
    return {"service_status": "responding", "database_status": await asyncio.to_thread(check),
            "business_health": "unknown", "source_status": "unknown"}


async def dashboard(args, ctx):
    from src.services.dashboard_service import build_dashboard_snapshot
    service = await dependency(ctx, dependencies.get_task_service)
    snapshot = await build_dashboard_snapshot(await service.get_all_tasks())
    summary = snapshot.get("summary", {})
    allowed = ("enabled_tasks", "running_tasks", "result_files", "scanned_items", "recommended_items",
               "ai_recommended_items", "keyword_recommended_items")
    return {"summary": {**{key: summary.get(key) for key in allowed},
                        "last_updated_at": redact(summary.get("last_updated_at"), 64) or None}}


async def system_status(args, ctx):
    from src.mcp.admin_tools import get_settings_store, ai_state, notification_state
    output = {"process_status": "unknown", "running_task_ids": None,
              "running_task_count": None, "scheduler_status": "unknown",
              "ai_configured": None, "configured_notification_channels": None,
              "business_health": "unknown"}
    try:
        process = await dependency(ctx, dependencies.get_process_service)
        running = sorted(task_id for task_id, value in process.processes.items()
                         if type(task_id) is int and value and value.returncode is None)
        output.update(process_status="observed", running_task_ids=running[:100],
                      running_task_count=len(running), running_tasks_truncated=len(running) > 100)
    except Exception:
        pass
    try:
        await dependency(ctx, dependencies.get_scheduler_service)
        output["scheduler_status"] = "available"
    except Exception:
        pass
    try:
        store = await dependency(ctx, get_settings_store)
        ai = ai_state(store)
        output["ai_configured"] = bool(ai["backend_configured"] and ai["OPENAI_MODEL_NAME"])
        output["configured_notification_channels"] = notification_state(store)["configured_channels"]
    except Exception:
        pass
    return output


class TaskInput(Input):
    task_id: int = Field(ge=0)


class LogInput(TaskInput):
    offset_lines: int = Field(default=0, ge=0, le=10000)
    limit_lines: int = Field(default=50, ge=1, le=100)


async def task_for(args, ctx):
    service = await dependency(ctx, dependencies.get_task_service)
    task = await service.get_task(args.task_id)
    if task is None:
        raise HTTPException(404, "task_not_found")
    return task


async def task_logs(args, ctx):
    from src.utils import resolve_task_log_path
    task = await task_for(args, ctx)

    def read():
        root = Path("logs")
        path = Path(resolve_task_log_path(task.id, task.task_name))
        if root.is_symlink() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise HTTPException(400, "forbidden_log_path")
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return {"content": "", "has_more": False, "truncated": False,
                    "next_offset": args.offset_lines, "status": "missing"}
        with os.fdopen(fd, "rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            start = max(0, size - 65536)
            stream.seek(start)
            lines = stream.read(65536).decode("utf-8", errors="replace").splitlines()
        if start:
            lines = lines[1:]  # Drop a potentially credential-bearing partial line.
        safe_lines = redact_log_lines(lines, partial_start=bool(start))
        end = max(0, len(lines) - args.offset_lines)
        begin = max(0, end - args.limit_lines)
        selected = lines[begin:end]
        content = "\n".join(safe_lines[begin:end])
        encoded = content.encode("utf-8")
        return {"content": encoded[:32768].decode("utf-8", errors="ignore"),
                "has_more": bool(begin), "next_offset": args.offset_lines + len(selected),
                "truncated": bool(start or len(encoded) > 32768 or any(len(line) > 512 for line in selected)),
                "status": "observed", "max_read_bytes": 65536}
    return await asyncio.to_thread(read)


class MetadataPage(Input):
    page: int = Field(default=1, ge=1, le=100000)
    limit: int = Field(default=50, ge=1, le=100)


async def account_names(ctx):
    import re
    from src.mcp.admin_tools import get_settings_store
    store = await dependency(ctx, get_settings_store)
    root = Path(store.get_value("ACCOUNT_STATE_DIR", "state") or "state")
    if not root.exists():
        return []
    return sorted(path.stem for path in root.iterdir() if path.suffix == ".json"
                  and not path.is_symlink() and path.is_file()
                  and re.fullmatch(r"[A-Za-z0-9_-]{1,50}", path.stem))


async def list_accounts(args, ctx):
    names = await account_names(ctx)
    start = (args.page - 1) * args.limit
    return {"items": [{"name": redact(name, 50), "source": "local", "session_status": "unknown"}
                      for name in names[start:start + args.limit]],
            "total": len(names), "page": args.page, "limit": args.limit,
            "has_more": start + args.limit < len(names)}


async def account_source(args, ctx):
    task = await task_for(args, ctx)
    strategy = task.account_strategy
    result = {"task_id": task.id, "strategy": strategy if strategy in {"ass", "auto", "fixed", "rotate"} else "unknown",
              "source_status": "unknown", "session_status": "unknown", "error_code": None}
    if strategy == "ass":
        from src.infrastructure.credentials.ass_session_client import AssSessionSettings
        try:
            AssSessionSettings.from_env()  # Configuration validation only; never read the Key or call ASS.
            result["configuration_status"] = "configured"
        except Exception:
            result.update(configuration_status="invalid", error_code="ass_configuration_invalid")
    elif strategy in {"auto", "fixed", "rotate"}:
        if strategy == "fixed":
            path = Path(task.account_state_file) if task.account_state_file else None
            result["local_file_present"] = bool(path and path.is_file() and not path.is_symlink())
        else:
            names = await account_names(ctx)
            result["local_account_count"] = len(names)
            result["root_state_present"] = Path("xianyu_state.json").is_file()
        result["configuration_status"] = "observed"
    return result


async def failure_guard(args, ctx):
    import json
    from datetime import datetime
    task = await task_for(args, ctx)
    unknown = {"task_id": task.id, "status": "unknown", "error_code": "guard_state_unavailable"}
    try:
        process = await dependency(ctx, dependencies.get_process_service)
        guard = process.failure_guard
        path = Path(guard.path)
        # Do not call FailureGuard._load(): it renames malformed state files.
        if path.is_symlink():
            return unknown
        with path.open("rb") as stream:
            raw = stream.read(1048577)
        if len(raw) > 1048576:
            return unknown
        entry = json.loads(raw)["tasks"].get(task.task_name)
        if not isinstance(entry, dict):
            return unknown
        def timestamp(key):
            value = entry.get(key)
            if value is None:
                return None
            return datetime.fromisoformat(value).isoformat()
        failures = entry.get("consecutive_failures")
        if type(failures) is not int or failures < 0:
            return unknown
        return {"task_id": task.id, "status": "observed", "consecutive_failures": failures,
                "threshold": int(guard.threshold), "pause_seconds": int(guard.pause_seconds),
                "paused_until": timestamp("paused_until"), "last_failure_at": timestamp("last_failure_at"),
                "last_success_at": timestamp("last_success_at"),
                "last_failure_code": "recorded_failure" if entry.get("last_failure_reason") else None}
    except Exception:
        return unknown


def build_tools():
    return [
        tool("get_failure_guard_status", "Read bounded failure-guard evidence for a task without mutating it; reason text and credential paths are withheld.", TaskInput, "system:read", failure_guard),
        tool("list_account_metadata", "List bounded local account names only; never opens cookie files or returns paths.", MetadataPage, "accounts:read", list_accounts),
        tool("get_account_source_status", "Inspect task account-source configuration without reading credentials or contacting ASS; session health remains unknown.", TaskInput, "accounts:read", account_source),
        tool("get_task_logs", "Read at most 100 redacted lines from a task's fixed local log; bounded 64 KiB source window/32 KiB output, reports truncation.", LogInput, "system:read", task_logs),
        tool("get_dashboard_summary", "Read aggregate counts from existing tasks/results, without raw records or secrets.", Input, "system:read", dashboard),
        tool("get_system_status", "Read process/configuration evidence; unavailable components remain unknown.", Input, "system:read", system_status),
        tool("get_health", "Inspect local API/database evidence; upstream and business health remain unknown without probes.", Input, "system:read", health),
    ]
