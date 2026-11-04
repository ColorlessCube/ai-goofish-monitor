"""Diagnostics inspect local fixtures, never ASS or websites."""
import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from src.api import dependencies
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.services.task_service import TaskService


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    import socket
    def denied(*args, **kwargs):
        raise AssertionError("network forbidden in MCP diagnostic unit tests")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture
def diagnostic_context(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts/base_prompt.txt").write_text("Base {{CRITERIA_SECTION}}")
    (tmp_path / "prompts/fixture_criteria.txt").write_text("fixture criteria")
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "diag.sqlite3"))
    repository = SqliteTaskRepository(db_path=str(tmp_path / "diag.sqlite3"), legacy_config_file=None)
    service = TaskService(repository)
    asyncio.run(service.get_all_tasks())
    app = FastAPI()
    app.dependency_overrides[dependencies.get_task_service] = lambda: service
    def missing():
        raise RuntimeError("credential=secret https://private.invalid")
    app.dependency_overrides[dependencies.get_process_service] = missing
    app.dependency_overrides[dependencies.get_scheduler_service] = missing
    return {"app": app, "principal": {"subject": "unit", "scopes": {"system:read", "accounts:read"}},
            "service": service, "root": tmp_path}


def call(ctx, name, **arguments):
    module = importlib.import_module("src.mcp.diagnostic_tools")
    tool = next(t for t in module.build_tools() if t["name"] == name)
    return asyncio.run(tool["handler"](tool["input_model"](**arguments), ctx))


def test_health_does_not_invent_business_health(diagnostic_context):
    result = call(diagnostic_context, "get_health")
    assert result["service_status"] == "responding"
    assert result["database_status"] == "available"
    assert result["business_health"] == "unknown"
    assert result["source_status"] == "unknown"
    assert "secret" not in json.dumps(result)


def create_task(ctx, **kwargs):
    from src.domain.models.task import TaskCreate
    return asyncio.run(ctx["service"].create_task(TaskCreate(task_name="fixture", keyword="camera", description="fixture criteria", ai_prompt_criteria_file="prompts/fixture_criteria.txt", **kwargs)))


def test_dashboard_and_system_report_observed_counts_only(diagnostic_context):
    create_task(diagnostic_context)
    snapshot = call(diagnostic_context, "get_dashboard_summary")
    assert snapshot["summary"]["enabled_tasks"] == 1
    assert snapshot["summary"]["scanned_items"] == 0
    unknown = call(diagnostic_context, "get_system_status")
    assert unknown["process_status"] == "unknown"
    assert unknown["running_task_ids"] is None
    diagnostic_context["app"].dependency_overrides[dependencies.get_process_service] = lambda: SimpleNamespace(
        processes={0: SimpleNamespace(returncode=None), 1: SimpleNamespace(returncode=0)})
    status = call(diagnostic_context, "get_system_status")
    assert status["process_status"] == "observed" and status["running_task_ids"] == [0]
    assert "secret" not in json.dumps(status)


def test_task_logs_are_bounded_redacted_and_not_arbitrary_paths(diagnostic_context):
    task = create_task(diagnostic_context)
    root = diagnostic_context["root"]
    (root / "logs").mkdir()
    (root / "logs" / f"fixture_{task.id}.log").write_text(
        "ordinary line\nCookie: sid=fixture-cookie\nAuthorization: Bearer fixture-token\n"
        "OPENAI_API_KEY=fixture-key\nhttps://user:pass@example.invalid/webhook\n" + "x" * 90000 + "\n2026-01-01 00:00:00 INFO last safe line\n")
    result = call(diagnostic_context, "get_task_logs", task_id=task.id, limit_lines=100)
    assert len(result["content"].encode()) <= 32768
    assert "fixture-cookie" not in result["content"] and "fixture-token" not in result["content"]
    assert "fixture-key" not in result["content"] and "user:pass" not in result["content"]
    assert "last safe line" in result["content"] and result["truncated"] is True
    with pytest.raises(ValidationError):
        call(diagnostic_context, "get_task_logs", task_id=task.id, path="/etc/passwd")
    (root / "logs" / f"fixture_{task.id}.log").unlink()
    (root / "outside.log").write_text("fixture secret outside")
    (root / "logs" / f"fixture_{task.id}.log").symlink_to(root / "outside.log")
    with pytest.raises(HTTPException) as exc:
        call(diagnostic_context, "get_task_logs", task_id=task.id)
    assert exc.value.status_code == 400


def test_account_metadata_and_source_never_read_cookie_values(diagnostic_context, monkeypatch):
    from pathlib import Path
    from src.mcp.admin_tools import get_settings_store
    from src.infrastructure.config.env_manager import EnvManager
    root = diagnostic_context["root"]
    store = EnvManager(str(root / ".env"))
    store.set_value("ACCOUNT_STATE_DIR", str(root / "state"))
    diagnostic_context["app"].dependency_overrides[get_settings_store] = lambda: store
    (root / "state").mkdir()
    account = root / "state" / "main.json"
    account.write_text('{"cookies":[{"value":"fixture-cookie"}]}')
    original = Path.read_text
    def protect(path, *args, **kwargs):
        assert path != account, "account metadata must never open cookie files"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", protect)
    result = call(diagnostic_context, "list_account_metadata", limit=1)
    assert result["items"] == [{"name": "main", "source": "local", "session_status": "unknown"}]
    assert result["total"] == 1
    task = create_task(diagnostic_context, account_strategy="ass")
    monkeypatch.delenv("ASS_ORIGIN", raising=False)
    source = call(diagnostic_context, "get_account_source_status", task_id=task.id)
    assert source["strategy"] == "ass" and source["source_status"] == "unknown"
    assert source["error_code"] == "ass_configuration_invalid"
    assert "fixture-cookie" not in json.dumps(source)


def test_failure_guard_returns_safe_state_and_does_not_repair_files(diagnostic_context):
    task = create_task(diagnostic_context)
    path = diagnostic_context["root"] / "guard.json"
    guard = SimpleNamespace(path=str(path), threshold=3, pause_seconds=86400)
    diagnostic_context["app"].dependency_overrides[dependencies.get_process_service] = lambda: SimpleNamespace(failure_guard=guard)
    path.write_text(json.dumps({"tasks": {task.task_name: {
        "consecutive_failures": 3, "paused_until": "2026-01-01T00:00:00+08:00",
        "last_failure_reason": "cookie=fixture-secret", "cookie_path": "/private/fixture",
        "last_failure_at": "2026-01-01T00:00:00+08:00"}}}))
    result = call(diagnostic_context, "get_failure_guard_status", task_id=task.id)
    assert result["status"] == "observed" and result["consecutive_failures"] == 3
    assert result["last_failure_code"] == "recorded_failure"
    assert "fixture-secret" not in json.dumps(result) and "/private" not in json.dumps(result)
    path.write_text("{bad json fixture-secret")
    result = call(diagnostic_context, "get_failure_guard_status", task_id=task.id)
    assert result["status"] == "unknown" and result["error_code"] == "guard_state_unavailable"
    assert path.read_text() == "{bad json fixture-secret"
    assert not list(path.parent.glob("guard.json.corrupt*"))


def test_source_http_errors_do_not_echo_credentials(diagnostic_context):
    def broken():
        raise HTTPException(503, "Authorization: Bearer rawfixturecredential")
    diagnostic_context["app"].dependency_overrides[dependencies.get_task_service] = broken
    with pytest.raises(HTTPException) as exc:
        call(diagnostic_context, "get_dashboard_summary")
    assert exc.value.status_code == 503
    assert exc.value.detail == "source_unavailable"


def test_log_redaction_covers_multiline_cookie_dump(diagnostic_context):
    task = create_task(diagnostic_context)
    log = diagnostic_context["root"] / "logs" / f"fixture_{task.id}.log"
    log.parent.mkdir()
    log.write_text('ordinary line\n{"cookies": [\n{"name":"sid", "value":"rawfixturecredential"}\n]}\n'
                   'Authorization: Bearer rawbearerfixture\napi_key=rawapikeyfixture\n')
    result = call(diagnostic_context, "get_task_logs", task_id=task.id)
    assert "rawfixturecredential" not in result["content"]
    assert "rawbearerfixture" not in result["content"]
    assert "rawapikeyfixture" not in result["content"]
    assert "ordinary line" in result["content"]


def test_log_pagination_does_not_promise_unreachable_lines(diagnostic_context):
    task = create_task(diagnostic_context)
    log = diagnostic_context["root"] / "logs" / f"fixture_{task.id}.log"
    log.parent.mkdir()
    log.write_text("x" * 90000 + "\nlast line\n")
    result = call(diagnostic_context, "get_task_logs", task_id=task.id, offset_lines=10)
    assert result["content"] == "" and result["truncated"] is True
    assert result["has_more"] is False
