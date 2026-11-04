"""Real socket HTTP CRUD against disposable SQLite, no crawler/AI/network fixtures."""
import importlib.util
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn


@pytest.fixture()
def isolated_mcp_http(monkeypatch, tmp_path, api_context):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GOOFISH_MCP_ENABLED", "true")
    monkeypatch.setenv("GOOFISH_MCP_ALLOWED_HOSTS", "127.0.0.1")
    monkeypatch.setenv("GOOFISH_MCP_READ_TOKEN", "read-socket-fixture-" + "r" * 40)
    monkeypatch.setenv("GOOFISH_MCP_WRITE_TOKEN", "write-socket-fixture-" + "w" * 40)
    monkeypatch.setenv("GOOFISH_MCP_WRITE_SCOPES", "tasks:read,tasks:write,tasks:delete,system:read")
    monkeypatch.setenv("GOOFISH_MCP_STATE_DIR", str(tmp_path / "receipts"))
    from src.api.auth import ApiAuthorizationMiddleware
    from src.mcp.remote import router
    app = api_context["app"]
    app.include_router(router)
    app.add_middleware(ApiAuthorizationMiddleware)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "fixture server did not start"
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive(), "fixture server did not exit"


def test_release_smoke_runs_real_mcp_crud(isolated_mcp_http, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts" / "smoke_native_mcp.py"
    spec = importlib.util.spec_from_file_location("smoke_native_mcp", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hasattr(module, "run_smoke"), "CRUD release smoke implementation is missing"
    receipt = module.run_smoke(isolated_mcp_http, "read-socket-fixture-" + "r" * 40, "write-socket-fixture-" + "w" * 40)
    assert receipt["status"] == "passed"
    assert receipt["remaining_tasks"] == 0
    assert receipt["write_replay_verified"] is True
    assert receipt["stale_revision_rejected"] is True
