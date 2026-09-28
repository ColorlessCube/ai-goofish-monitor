from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.api.auth import ApiAuthorizationMiddleware, issue_admin_session
from src.api.routes import websocket as websocket_routes


def test_password_login_issues_an_httponly_admin_session(monkeypatch):
    monkeypatch.setenv("GOOFISH_SESSION_SECRET", "test-session-secret")
    from src.app import app

    client = TestClient(app)
    response = client.post("/auth/status", json={"username": "admin", "password": "admin123"})

    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    assert "goofish_session=" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]


def test_websocket_requires_an_admin_session(monkeypatch):
    monkeypatch.setenv("GOOFISH_SESSION_SECRET", "test-session-secret")
    app = FastAPI()
    app.include_router(websocket_routes.router)
    client = TestClient(app)

    try:
        with client.websocket_connect("/ws"):
            raise AssertionError("unauthenticated websocket was accepted")
    except WebSocketDisconnect as error:
        assert error.code == 4401

    client.cookies.set("goofish_session", issue_admin_session("admin"))
    with client.websocket_connect("/ws"):
        pass


def _client(monkeypatch):
    monkeypatch.setenv("GOOFISH_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("GOOFISH_MCP_READ_TOKEN", "test-read-token")
    app = FastAPI()
    app.add_middleware(ApiAuthorizationMiddleware)

    @app.get("/health")
    async def health():
        return {"status": "healthy"}

    @app.get("/api/readonly")
    async def readonly():
        return {"ok": True}

    @app.post("/api/readonly")
    async def write_attempt():
        return {"ok": True}

    @app.post("/api/admin-action")
    async def admin_action():
        return {"ok": True}

    return TestClient(app)


def test_health_is_anonymous_but_api_is_denied_without_credentials(monkeypatch):
    client = _client(monkeypatch)

    assert client.get("/health").status_code == 200
    assert client.get("/api/readonly").status_code == 401


def test_read_service_token_allows_only_safe_reads(monkeypatch):
    client = _client(monkeypatch)
    headers = {"Authorization": "Bearer test-read-token"}

    assert client.get("/api/readonly", headers=headers).status_code == 200
    assert client.post("/api/readonly", headers=headers).status_code == 403
    assert client.get("/api/admin-action", headers=headers).status_code == 403


def test_admin_session_allows_write_operations(monkeypatch):
    client = _client(monkeypatch)
    token = issue_admin_session("admin")
    client.cookies.set("goofish_session", token)

    assert client.post("/api/admin-action").status_code == 200
