"""Side-effect requests must never follow redirects; all transports are fake."""
import asyncio
import json
import os
import socket
from urllib.parse import urlsplit

import pytest
import requests
from requests.adapters import HTTPAdapter


REDIRECT_LOCATION_SECRET = "fixture-location-must-not-escape"
CHANNEL_PATHS = [
    "bark", "gotify", "ntfy", "telegram", "wecom",
    "webhook-post", "webhook-form", "webhook-get",
]
ACK = {"code": 200, "errcode": 0, "ok": True, "id": 1}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith(("OPENAI_", "ASS_", "WEBHOOK_", "TELEGRAM_", "GOTIFY_",
                           "NTFY_", "BARK_", "WX_", "PROXY_", "GOOFISH_", "APP_DATABASE_")) or key.lower().endswith("_proxy"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "fixture.sqlite3"))
    monkeypatch.setenv("GOOFISH_MCP_STATE_DIR", str(tmp_path / "journal"))

    def denied(*args, **kwargs):
        raise AssertionError("real network forbidden")

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def notification_client(channel_path):
    from src.infrastructure.external.notification_clients.bark_client import BarkClient
    from src.infrastructure.external.notification_clients.gotify_client import GotifyClient
    from src.infrastructure.external.notification_clients.ntfy_client import NtfyClient
    from src.infrastructure.external.notification_clients.telegram_client import TelegramClient
    from src.infrastructure.external.notification_clients.wecom_bot_client import WeComBotClient
    from src.infrastructure.external.notification_clients.webhook_client import WebhookClient

    clients = {
        "bark": lambda: BarkClient("https://saved.invalid/bark"),
        "gotify": lambda: GotifyClient("https://saved.invalid", "fixture-token"),
        "ntfy": lambda: NtfyClient("https://saved.invalid/topic"),
        "telegram": lambda: TelegramClient("fixture-token", "fixture-chat", "https://saved.invalid"),
        "wecom": lambda: WeComBotClient("https://saved.invalid/wecom"),
    }
    if channel_path in clients:
        return clients[channel_path]()
    return WebhookClient(
        "https://saved.invalid/hook",
        webhook_method="GET" if channel_path == "webhook-get" else "POST",
        webhook_content_type="FORM" if channel_path == "webhook-form" else "JSON",
        webhook_headers='{"X-Api-Key":"fixture-custom-header"}',
        webhook_query_parameters='{"message":"fixture-query"}',
        webhook_body='{"message":"fixture-body"}',
    )


def notification_transport(monkeypatch, channel_path, status, location=None):
    """Exercise requests.Session redirect handling, not a mocked requests.post."""
    calls = []

    def send(self, request, **kwargs):
        calls.append({"method": request.method, "url": request.url,
                      "headers": dict(request.headers), "body": request.body})
        response = requests.Response()
        response.request = request
        response.url = request.url
        response.status_code = status if len(calls) == 1 else 200
        ack = {"id": "fixture-id", "event": "message"} if channel_path == "ntfy" else ACK
        response._content = json.dumps(ack).encode()
        if len(calls) == 1 and location:
            response.headers["Location"] = location
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    return calls


def assert_unknown(value):
    assert value["success"] is False
    assert value["attempted"] is True and value["outcome"] == "unknown"
    assert value["retry_allowed"] is False
    assert REDIRECT_LOCATION_SECRET not in json.dumps(value)


@pytest.mark.parametrize("channel_path", CHANNEL_PATHS)
@pytest.mark.parametrize("status", [307, 308])
@pytest.mark.parametrize("redirect_host", ["saved.invalid", "untrusted.invalid"], ids=["same-origin", "cross-origin"])
def test_notification_redirect_never_resends(monkeypatch, channel_path, status, redirect_host):
    from src.services.notification_service import NotificationService

    location = f"https://{redirect_host}/again?token={REDIRECT_LOCATION_SECRET}"
    calls = notification_transport(monkeypatch, channel_path, status, location)
    client = notification_client(channel_path)
    result = asyncio.run(NotificationService([client]).send_test_notification())[client.channel_key]

    assert len(calls) == 1, calls
    assert urlsplit(calls[0]["url"]).netloc == "saved.invalid"
    assert calls[0]["url"] != location
    assert calls[0]["method"] == ("GET" if channel_path == "webhook-get" else "POST")
    if channel_path.startswith("webhook"):
        assert calls[0]["headers"]["X-Api-Key"] == "fixture-custom-header"
        if channel_path != "webhook-get":
            assert "fixture-body" in str(calls[0]["body"])
    assert_unknown(result)


def sdk_transport(monkeypatch, status, location=None):
    """Use the installed SDK's HTTP implementation (httpx or httpx2)."""
    import importlib
    import openai

    http_base = next(base for base in openai.DefaultHttpxClient.__mro__
                     if base.__module__.split(".")[0] in {"httpx", "httpx2"})
    http = importlib.import_module(http_base.__module__.split(".")[0])
    calls, clients = [], []
    original_init = openai.OpenAI.__init__

    def initialized(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        clients.append(self)

    def handle_request(self, request):
        calls.append({"method": request.method, "url": str(request.url),
                      "headers": dict(request.headers), "body": request.read()})
        if len(calls) == 1 and location:
            return http.Response(status, headers={"location": location}, request=request)
        return http.Response(status if len(calls) == 1 else 200, request=request, json={
            "id": "fixture-id", "object": "chat.completion", "created": 1,
            "model": "fixture-model", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
        })

    monkeypatch.setattr(openai.OpenAI, "__init__", initialized)
    monkeypatch.setattr(http.HTTPTransport, "handle_request", handle_request)
    return calls, clients


def ai_settings(proxy):
    return {"OPENAI_API_KEY": "fixture-ai-key", "OPENAI_BASE_URL": "https://saved.invalid/v1",
            "OPENAI_MODEL_NAME": "fixture-model", "PROXY_URL": proxy}


def assert_sdk_closed_and_no_retries(clients):
    assert len(clients) == 1
    assert clients[0].max_retries == 0
    assert clients[0]._client.follow_redirects is False
    assert clients[0]._client.is_closed is True


@pytest.mark.parametrize("proxy", ["", "http://proxy.invalid:8080"], ids=["direct", "proxy"])
@pytest.mark.parametrize("status", [307, 308])
@pytest.mark.parametrize("redirect_host", ["saved.invalid", "untrusted.invalid"], ids=["same-origin", "cross-origin"])
def test_ai_redirect_never_resends(monkeypatch, proxy, status, redirect_host):
    from src.api.routes import settings as rest

    location = f"https://{redirect_host}/again?token={REDIRECT_LOCATION_SECRET}"
    calls, clients = sdk_transport(monkeypatch, status, location)
    result = asyncio.run(rest.test_ai_settings(ai_settings(proxy)))
    assert len(calls) == 1, {"calls": calls, "result": result}
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "https://saved.invalid/v1/chat/completions"
    assert calls[0]["headers"]["authorization"] == "Bearer fixture-ai-key"
    assert json.loads(calls[0]["body"])["model"] == "fixture-model"
    assert_unknown(result)
    assert_sdk_closed_and_no_retries(clients)


@pytest.mark.parametrize("proxy", ["", "http://proxy.invalid:8080"], ids=["direct", "proxy"])
def test_ai_success_uses_explicit_safe_client_and_closes_it(monkeypatch, proxy):
    from src.api.routes import settings as rest

    calls, clients = sdk_transport(monkeypatch, 200)
    result = asyncio.run(rest.test_ai_settings(ai_settings(proxy)))
    assert len(calls) == 1, result
    assert result["success"] is True and result["outcome"] == "accepted"
    assert result["response"] == "OK"
    assert_sdk_closed_and_no_retries(clients)


@pytest.mark.parametrize("channel_path", CHANNEL_PATHS)
@pytest.mark.parametrize("status,outcome", [(302, "unknown"), (400, "rejected"), (500, "unknown")])
def test_notification_success_body_cannot_override_http_status(monkeypatch, channel_path, status, outcome):
    from src.services.notification_service import NotificationService

    calls = notification_transport(monkeypatch, channel_path, status)
    client = notification_client(channel_path)
    result = asyncio.run(NotificationService([client]).send_test_notification())[client.channel_key]
    assert len(calls) == 1
    assert result["success"] is False and result["outcome"] == outcome
    assert result["attempted"] is True and result["retry_allowed"] is False


@pytest.mark.parametrize("channel_path", CHANNEL_PATHS)
def test_notification_preserves_legacy_send_and_boolean_service_contract(monkeypatch, channel_path):
    from src.services.notification_service import NotificationService

    calls = notification_transport(monkeypatch, channel_path, 200)
    client = notification_client(channel_path)
    # The existing concrete adapters return None on success, not a result object.
    assert asyncio.run(client.send({"商品标题": "fixture"}, "fixture")) is None
    assert len(calls) == 1
    calls.clear()
    result = asyncio.run(NotificationService([client]).send_test_notification())[client.channel_key]
    assert len(calls) == 1
    assert result["success"] is True and result["outcome"] == "accepted"


@pytest.mark.parametrize("proxy", ["", "http://proxy.invalid:8080"], ids=["direct", "proxy"])
@pytest.mark.parametrize("status", [408, 500])
def test_ai_http_uncertainty_is_not_retried(monkeypatch, proxy, status):
    from src.api.routes import settings as rest

    calls, clients = sdk_transport(monkeypatch, status)
    result = asyncio.run(rest.test_ai_settings(ai_settings(proxy)))
    assert len(calls) == 1
    assert_unknown(result)
    assert_sdk_closed_and_no_retries(clients)


@pytest.mark.parametrize("proxy", ["", "http://proxy.invalid:8080"], ids=["direct", "proxy"])
def test_ai_http_client_is_closed_if_sdk_initialization_fails(monkeypatch, proxy):
    import openai
    from src.api.routes import settings as rest

    clients = []
    original = openai.DefaultHttpxClient

    def tracked_client(**kwargs):
        client = original(**kwargs)
        clients.append(client)
        return client

    def failed_sdk(**kwargs):
        raise ValueError(REDIRECT_LOCATION_SECRET)

    monkeypatch.setattr(openai, "DefaultHttpxClient", tracked_client)
    monkeypatch.setattr(openai, "OpenAI", failed_sdk)
    result = asyncio.run(rest.test_ai_settings(ai_settings(proxy)))
    assert result["success"] is False and result["attempted"] is False
    assert result["outcome"] == "not_attempted"
    assert REDIRECT_LOCATION_SECRET not in json.dumps(result)
    assert len(clients) == 1 and clients[0].is_closed


@pytest.mark.parametrize("path", CHANNEL_PATHS + ["ai-direct", "ai-proxy"])
@pytest.mark.parametrize("status", [307, 308])
@pytest.mark.parametrize("redirect_host", ["saved.invalid", "untrusted.invalid"], ids=["same-origin", "cross-origin"])
def test_redirect_receipt_stays_unknown_on_replay_and_disk(tmp_path, monkeypatch, path, status, redirect_host):
    from uuid import uuid4
    from fastapi import FastAPI
    from src.infrastructure.config.env_manager import EnvManager
    from src.mcp import admin_tools as admin, journal
    from src.mcp.remote import execute_mutation

    location = f"https://{redirect_host}/again?token={REDIRECT_LOCATION_SECRET}"
    values = {key: "" for fields in admin.CHANNEL_REQUIREMENTS.values() for key in fields}
    clients = []
    values.update({"BARK_URL": "https://saved.invalid/bark", "GOTIFY_URL": "https://saved.invalid",
                   "GOTIFY_TOKEN": "fixture-token", "NTFY_TOPIC_URL": "https://saved.invalid/topic",
                   "TELEGRAM_BOT_TOKEN": "fixture-token", "TELEGRAM_CHAT_ID": "fixture-chat",
                   "TELEGRAM_API_BASE_URL": "https://saved.invalid", "WX_BOT_URL": "https://saved.invalid/wecom",
                   "WEBHOOK_URL": "https://saved.invalid/hook", "PROXY_URL": "",
                   "WEBHOOK_METHOD": "GET" if path == "webhook-get" else "POST",
                   "WEBHOOK_CONTENT_TYPE": "FORM" if path == "webhook-form" else "JSON",
                   "WEBHOOK_HEADERS": '{"X-Api-Key":"fixture-custom-header"}',
                   "WEBHOOK_BODY": '{"message":"fixture-body"}'})
    if path.startswith("ai-"):
        values.update(ai_settings("http://proxy.invalid:8080" if path == "ai-proxy" else ""))
        calls, clients = sdk_transport(monkeypatch, status, location)
        name, arguments = "test_ai_connection", {}
    else:
        calls = notification_transport(monkeypatch, path, status, location)
        name, arguments = "test_notification", {"channel": path.split("-")[0]}
    store = EnvManager(str(tmp_path / ".env"))
    assert store.update_values(values) is True
    app = FastAPI()
    app.dependency_overrides[admin.get_settings_store] = lambda: store
    ctx = {"app": app, "principal": {"subject": "redirect-review", "scopes": {"settings:test", "notifications:send"}}}
    spec = next(tool for tool in admin.build_tools() if tool["name"] == name)
    request_id = str(uuid4())

    def invoke():
        return asyncio.run(execute_mutation(spec, spec["input_model"](**arguments), arguments, request_id, ctx))

    first = invoke()
    value = json.loads(first["content"][0]["text"])
    assert_unknown(value)
    assert value["_receipt"]["status"] == "unknown"
    assert invoke() == first
    assert len(calls) == 1 and calls[0]["url"] != location
    receipt = journal.get_receipt(ctx["principal"]["subject"], request_id)
    assert receipt["status"] == "unknown" and receipt["retry_allowed"] is False
    assert receipt["result"] == first
    with journal.connection() as db:
        row = db.execute("SELECT status,response_json FROM invocations WHERE request_id=?", (request_id,)).fetchone()
    assert row is not None
    assert row["status"] == "unknown" and json.loads(row["response_json"]) == first
    assert REDIRECT_LOCATION_SECRET not in row["response_json"]
    if path.startswith("ai-"):
        assert_sdk_closed_and_no_retries(clients)
