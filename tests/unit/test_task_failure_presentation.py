"""Task faults are actionable alerts, not product recommendations; no real sends."""
import asyncio
import json
import socket
from unittest.mock import Mock

import pytest
import requests

from src.infrastructure.external.notification_clients import base
from src.infrastructure.external.notification_clients.bark_client import BarkClient


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("real network forbidden")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def test_task_fault_message_preserves_actionable_text_without_product_metadata(monkeypatch):
    title = "闲鱼登录失效｜这是一个超过三十个字符的监控任务名称，请重新登录后再启动任务"
    reason = "登录状态已失效。\n请到账号管理重新登录闲鱼，然后重新启动此任务。"
    convert = Mock(side_effect=AssertionError("fault must not derive a product link"))
    monkeypatch.setattr(base, "convert_goofish_link", convert)
    product = {
        "notification_type": "task_failure",
        "商品标题": title,
        "当前售价": "N/A",
        "商品链接": "https://www.goofish.com/item?id=123",
        "商品主图链接": "https://fixture.invalid/main.jpg",
        "商品图片列表": ["https://fixture.invalid/fallback.jpg"],
    }

    message = BarkClient("https://fixture.invalid/bark")._build_message(product, reason)

    assert message.title == message.notification_title == title
    assert message.reason == message.content == reason
    assert message.price == ""
    assert message.desktop_link == ""
    assert message.mobile_link is None
    assert message.image_url is None
    convert.assert_not_called()


def bark_payload(monkeypatch, product, reason, pcurl_to_mobile=True):
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps({"code": 200}).encode()
    post = Mock(return_value=response)
    monkeypatch.setattr(requests, "post", post)

    client = BarkClient("https://fixture.invalid/bark", pcurl_to_mobile=pcurl_to_mobile)
    assert asyncio.run(client.send(product, reason)) is None

    post.assert_called_once()
    args, kwargs = post.call_args
    assert args == ("https://fixture.invalid/bark",)
    assert kwargs["headers"] == {"Content-Type": "application/json; charset=utf-8"}
    assert kwargs["timeout"] == 10
    assert kwargs["allow_redirects"] is False
    return kwargs["json"]


@pytest.mark.parametrize("link", ["#", "https://www.goofish.com/item?id=123"])
@pytest.mark.parametrize("pcurl_to_mobile", [True, False])
def test_bark_task_fault_payload_has_only_actionable_text(monkeypatch, link, pcurl_to_mobile):
    title = "闲鱼登录失效｜这是一个超过三十个字符的监控任务名称，请重新登录后再启动任务"
    reason = "登录状态已失效。\n请到账号管理重新登录闲鱼，然后重新启动此任务。"
    payload = bark_payload(monkeypatch, {
        "notification_type": "task_failure",
        "商品标题": title,
        "当前售价": "N/A",
        "商品链接": link,
        "商品主图链接": "https://fixture.invalid/product.jpg",
        "商品图片列表": ["https://fixture.invalid/fallback.jpg"],
    }, reason, pcurl_to_mobile)

    assert payload == {
        "title": title,
        "body": reason,
        "level": "timeSensitive",
        "group": "闲鱼监控",
    }


@pytest.mark.parametrize("pcurl_to_mobile", [True, False])
@pytest.mark.parametrize("image_fields,expected_icon", [
    ({"商品主图链接": "https://fixture.invalid/main.jpg",
      "商品图片列表": ["https://fixture.invalid/fallback.jpg"]}, "https://fixture.invalid/main.jpg"),
    ({"商品图片列表": ["https://fixture.invalid/fallback.jpg"]}, "https://fixture.invalid/fallback.jpg"),
    ({}, None),
])
def test_bark_product_recommendation_payload_is_unchanged(monkeypatch, pcurl_to_mobile, image_fields, expected_icon):
    desktop_link = "https://www.goofish.com/item?id=123"
    mobile_link = (
        "https://pages.goofish.com/sharexy?loadingVisible=false&bft=item&bfs=idlepc.item"
        "&spm=a21ybx.item.0.0&bfp=%7B%22id%22%3A123%7D"
    )
    payload = bark_payload(monkeypatch, {
        "商品标题": "123456789012345678901234567890额外商品标题",
        "当前售价": "¥123",
        "商品链接": desktop_link,
        **image_fields,
    }, "值得购买", pcurl_to_mobile)

    content = "价格: ¥123\n原因: 值得购买\n"
    if pcurl_to_mobile:
        content += f"手机端链接: {mobile_link}\n电脑端链接: {desktop_link}"
    else:
        content += f"链接: {desktop_link}"
    expected = {
        "title": "🚨 新推荐! 123456789012345678901234567890...",
        "body": content,
        "url": mobile_link if pcurl_to_mobile else desktop_link,
        "level": "timeSensitive",
        "group": "闲鱼监控",
    }
    if expected_icon:
        expected["icon"] = expected_icon
    assert payload == expected


@pytest.mark.parametrize("link_fields,expected_link", [
    ({}, "#"),
    ({"商品链接": "#"}, "#"),
    ({"商品链接": ""}, ""),
    ({"商品链接": None}, None),
])
def test_bark_product_placeholder_link_behavior_is_unchanged(monkeypatch, link_fields, expected_link):
    payload = bark_payload(monkeypatch, {"商品标题": "短商品标题", **link_fields}, "推荐原因")

    assert payload == {
        "title": "🚨 新推荐! 短商品标题",
        "body": f"价格: N/A\n原因: 推荐原因\n链接: {expected_link}",
        "url": expected_link,
        "level": "timeSensitive",
        "group": "闲鱼监控",
    }
