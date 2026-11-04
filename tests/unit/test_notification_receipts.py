"""Exercise actual adapters with local HTTP fixtures; no real sends."""
import asyncio
import json

import pytest
import requests

from src.infrastructure.external.notification_clients.bark_client import BarkClient
from src.infrastructure.external.notification_clients.gotify_client import GotifyClient
from src.infrastructure.external.notification_clients.ntfy_client import NtfyClient
from src.infrastructure.external.notification_clients.telegram_client import TelegramClient
from src.infrastructure.external.notification_clients.wecom_bot_client import WeComBotClient
from src.infrastructure.external.notification_clients.webhook_client import WebhookClient
from src.services.notification_service import NotificationService


CLIENTS = [
    (BarkClient('https://fixture.invalid/bark'), {'code': 200}, {'code': 400}),
    (WeComBotClient('https://fixture.invalid/wecom'), {'errcode': 0}, {'errcode': 400}),
    (TelegramClient('fixture-token', 'fixture-chat'), {'ok': True}, {'ok': False}),
    (GotifyClient('https://fixture.invalid', 'fixture-token'), {'id': 1}, {}),
    (NtfyClient('https://fixture.invalid/topic'), {'id': 'fixture-id', 'event': 'message'}, {}),
]


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    import socket
    def denied(*args, **kwargs):
        raise AssertionError('real network forbidden')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)


def send(monkeypatch, client, body, status=200):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(body).encode()
    response.url = 'https://fixture.invalid/response'
    attempts = []
    def post(*args, **kwargs):
        attempts.append(1)
        return response
    monkeypatch.setattr(requests, 'post', post)
    result = asyncio.run(NotificationService([client]).send_test_notification())[client.channel_key]
    assert len(attempts) == 1
    return result


@pytest.mark.parametrize('client,accepted,rejected', CLIENTS)
def test_real_adapters_require_application_ack(monkeypatch, client, accepted, rejected):
    result = send(monkeypatch, client, rejected)
    assert result['success'] is False
    assert result['attempted'] is True and result['outcome'] in {'rejected', 'unknown'}
    assert result['retry_allowed'] is False
    result = send(monkeypatch, client, accepted)
    assert result['success'] is True and result['outcome'] == 'accepted'
    assert result['delivery_status'] == 'unknown'


@pytest.mark.parametrize('client,accepted,rejected', CLIENTS)
def test_missing_ack_is_not_success(monkeypatch, client, accepted, rejected):
    result = send(monkeypatch, client, {})
    assert result['success'] is False and result['outcome'] == 'unknown'


@pytest.mark.parametrize('body', [{'code': '200'}, {'code': True}, [], None])
def test_bark_wrong_ack_type_is_unknown(monkeypatch, body):
    result = send(monkeypatch, CLIENTS[0][0], body)
    assert result['success'] is False and result['outcome'] == 'unknown'


def test_read_timeout_is_unknown_and_never_retried(monkeypatch):
    attempts = []
    def post(*args, **kwargs):
        attempts.append(1)
        raise requests.ReadTimeout('fixture-private-error')
    monkeypatch.setattr(requests, 'post', post)
    client = WebhookClient('https://fixture.invalid/hook')
    result = asyncio.run(NotificationService([client]).send_test_notification())['webhook']
    assert result['attempted'] is True and result['outcome'] == 'unknown'
    assert result['retry_allowed'] is False and len(attempts) == 1
    assert 'fixture-private-error' not in json.dumps(result)


@pytest.mark.parametrize('status,outcome', [(204, 'accepted'), (400, 'rejected'), (500, 'unknown'), (302, 'unknown')])
def test_webhook_reports_http_boundary_not_user_delivery(monkeypatch, status, outcome):
    result = send(monkeypatch, WebhookClient('https://fixture.invalid/hook'), {}, status)
    assert result['outcome'] == outcome
    assert result['delivery_status'] == 'unknown'
