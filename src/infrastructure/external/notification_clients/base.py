"""
通知客户端基类
定义通知客户端的统一接口
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict

from src.utils import convert_goofish_link


class NotificationRejected(RuntimeError):
    """The receiver explicitly rejected this attempt; no response text escapes."""


class NotificationOutcomeUnknown(RuntimeError):
    """No conclusive acceptance/rejection acknowledgement was received."""


def validate_response(response, channel: str) -> None:
    """Validate the receiver's acceptance boundary, never claim user delivery."""
    status = getattr(response, "status_code", None)
    if type(status) is not int:
        raise NotificationOutcomeUnknown("acknowledgement_missing")
    if 400 <= status < 500 and status != 408:
        raise NotificationRejected("receiver_rejected")
    if not 200 <= status < 300:
        raise NotificationOutcomeUnknown("acceptance_unknown")
    if channel == "webhook":
        # Generic webhook schemas are unknown: only HTTP acceptance is evidence.
        return
    try:
        body = response.json()
    except Exception:
        raise NotificationOutcomeUnknown("acknowledgement_invalid") from None
    if not isinstance(body, dict):
        raise NotificationOutcomeUnknown("acknowledgement_invalid")
    fields = {"bark": ("code", 200, int), "wecom": ("errcode", 0, int),
              "telegram": ("ok", True, bool)}
    if channel in fields:
        key, accepted, expected_type = fields[channel]
        if type(body.get(key)) is not expected_type:
            raise NotificationOutcomeUnknown("acknowledgement_missing")
        if body[key] != accepted:
            raise NotificationRejected("receiver_rejected")
    elif channel == "gotify":
        if type(body.get("id")) is not int or body["id"] <= 0:
            raise NotificationOutcomeUnknown("acknowledgement_missing")
    elif channel == "ntfy":
        if not isinstance(body.get("id"), str) or not body["id"] or body.get("event") != "message":
            raise NotificationOutcomeUnknown("acknowledgement_missing")
    else:
        raise NotificationOutcomeUnknown("acknowledgement_unsupported")


@dataclass(frozen=True)
class NotificationMessage:
    title: str
    price: str
    reason: str
    desktop_link: str
    mobile_link: str | None
    notification_title: str
    content: str
    image_url: str | None


class NotificationClient(ABC):
    """通知客户端抽象基类"""

    channel_key = "unknown"
    display_name = "未知渠道"

    def __init__(self, enabled: bool = False, pcurl_to_mobile: bool = True):
        self._enabled = enabled
        self._pcurl_to_mobile = pcurl_to_mobile

    def is_enabled(self) -> bool:
        """检查客户端是否启用"""
        return self._enabled

    @abstractmethod
    async def send(self, product_data: Dict, reason: str) -> bool:
        """
        发送通知

        Args:
            product_data: 商品数据
            reason: 推荐原因

        Returns:
            是否发送成功
        """
        raise NotImplementedError

    def _build_message(self, product_data: Dict, reason: str) -> NotificationMessage:
        """格式化消息内容"""
        title = product_data.get('商品标题', 'N/A')
        if product_data.get('notification_type') == 'task_failure':
            return NotificationMessage(
                title=title,
                price="",
                reason=reason,
                desktop_link="",
                mobile_link=None,
                notification_title=title,
                content=reason,
                image_url=None,
            )

        price = product_data.get('当前售价', 'N/A')
        desktop_link = product_data.get('商品链接', '#')
        mobile_link = None

        if self._pcurl_to_mobile and desktop_link and desktop_link != "#":
            mobile_link = convert_goofish_link(desktop_link)

        content_lines = [
            f"价格: {price}",
            f"原因: {reason}",
        ]
        if mobile_link:
            content_lines.append(f"手机端链接: {mobile_link}")
            content_lines.append(f"电脑端链接: {desktop_link}")
        else:
            content_lines.append(f"链接: {desktop_link}")

        short_title = title[:30]
        suffix = "..." if len(title) > 30 else ""
        notification_title = f"🚨 新推荐! {short_title}{suffix}"

        main_image = product_data.get('商品主图链接')
        if not main_image:
            image_list = product_data.get('商品图片列表', [])
            if image_list:
                main_image = image_list[0]

        return NotificationMessage(
            title=title,
            price=price,
            reason=reason,
            desktop_link=desktop_link,
            mobile_link=mobile_link,
            notification_title=notification_title,
            content="\n".join(content_lines),
            image_url=main_image,
        )
